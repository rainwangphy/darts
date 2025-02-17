# coding: utf-8
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tensorboardX import SummaryWriter
from config import SearchConfig
from tools import utils
from models.search_cnn import SearchCNNController
from architect import Architect

# from tools.visualize import plot

os.environ["CUDA_VISIBLE_DEVICES"] = "3"

config = SearchConfig()

device = torch.device("cuda")

# tensorboard
tb_writer = SummaryWriter(log_dir=os.path.join(config.path, "tb"))
tb_writer.add_text('config', config.as_markdown(), 0)

logger = utils.get_logger(os.path.join(config.path, "{}.log".format(config.name)))
config.print_params(logger.info)


def main():
    logger.info("Logger is set - training start")

    torch.cuda.set_device(config.gpus[0])

    # seed setting
    np.random.seed(config.seed)
    torch.manual_seed(config.seed)
    torch.cuda.manual_seed_all(config.seed)

    torch.backends.cudnn.benchmark = True

    # get data with meta infomation
    input_size, input_channels, n_classes, train_data = utils.get_data(
        config.dataset, config.data_path, cutout_length=0, validation=False)

    # set model
    net_crit = nn.CrossEntropyLoss().to(device)
    model = SearchCNNController(input_channels, config.init_channels, n_classes, config.layers, net_crit,
                                n_nodes=config.nodes, device_ids=config.gpus)
    model = model.to(device)

    # weight optim
    w_optim = torch.optim.SGD(model.weights(), config.w_lr, momentum=config.w_momentum,
                              weight_decay=config.alpha_weight_decay)

    # alpha optim
    alpha_optim = torch.optim.Adam(model.alphas(), config.alpha_lr, betas=(0.5, 0.999),
                                   weight_decay=config.alpha_weight_decay)

    # split data (train,validation)
    n_train = len(train_data)
    split = n_train // 2
    indices = list(range(n_train))
    train_sampler = torch.utils.data.sampler.SubsetRandomSampler(indices[:split])
    valid_sampler = torch.utils.data.sampler.SubsetRandomSampler(indices[split:])

    train_loader = torch.utils.data.DataLoader(train_data,
                                               batch_size=config.batch_size,
                                               sampler=train_sampler,
                                               num_workers=config.workers,
                                               pin_memory=True)
    valid_loader = torch.utils.data.DataLoader(train_data,
                                               batch_size=config.batch_size,
                                               sampler=valid_sampler,
                                               num_workers=config.workers,
                                               pin_memory=True)

    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(w_optim, config.epochs, eta_min=config.w_lr_min)

    arch = Architect(model, config.w_momentum, config.w_weight_decay)

    # training loop-----------------------------------------------------------------------------
    best_top1 = 0.
    for epoch in range(config.epochs):

        lr = lr_scheduler.get_last_lr()[0]

        model.print_alphas(logger)

        # training
        train(train_loader, valid_loader, model, arch, w_optim, alpha_optim, lr, epoch)
        lr_scheduler.step()

        # validation
        cur_step = (epoch + 1) * len(train_loader)
        top1 = validate(valid_loader, model, epoch, cur_step)

        # log
        # genotype
        genotype = model.genotype()
        logger.info("genotype = {}".format(genotype))

        # genotype as a image
        # plot_path = os.path.join(config.plot_path, "EP{:02d}".format(epoch + 1))
        # caption = "Epoch {}".format(epoch + 1)
        # plot(genotype.normal, plot_path + "-normal", caption)
        # plot(genotype.reduce, plot_path + "-reduce", caption)

        # output alpha per epochs to tensorboard data
        # for i, tensor in enumerate(model.alpha_normal):
        #     for j, lsn in enumerate(F.softmax(tensor, dim=-1)):
        #         tb_writer.add_scalars('epoch_alpha_normal/%d ~~ %d' % ((j - 2), i),
        #                               {'max_pl3': lsn[0], 'avg_pl3': lsn[1], 'skip_cn': lsn[2], 'sep_conv3': lsn[3],
        #                                'sep_conv5': lsn[4], 'dil_conv3': lsn[5], 'dil_conv5': lsn[6], 'none': lsn[7]},
        #                               epoch)
        #     tb_writer.flush()
        # for i, tensor in enumerate(model.alpha_reduce):
        #     for j, lsr in enumerate(F.softmax(tensor, dim=-1)):
        #         tb_writer.add_scalars('epoch_alpha_reduce/%d ~~ %d' % ((j - 2), i),
        #                               {'max_pl3': lsr[0], 'avg_pl3': lsr[1], 'skip_cn': lsr[2], 'sep_conv3': lsr[3],
        #                                'sep_conv5': lsr[4], 'dil_conv3': lsr[5], 'dil_conv5': lsr[6], 'none': lsr[7]},
        #                               epoch)
        #     tb_writer.flush()
        # save
        if best_top1 < top1:
            best_top1 = top1
            best_genotype = genotype
            is_best = True
        else:
            is_best = False
        utils.save_checkpoint(model, config.path, is_best)
        print("")

    logger.info("Final best Prec@1 = {:.4%}".format(best_top1))
    logger.info("Best Genotype is = {}".format(best_genotype))
    tb_writer.close()


def train(train_loader, valid_loader, model, arch, w_optim, alpha_optim, lr, epoch):
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()
    losses = utils.AverageMeter()

    cur_step = epoch * len(train_loader)
    tb_writer.add_scalar('train/lr', lr, cur_step)

    model.train()

    for step, ((train_X, train_y), (valid_X, valid_y)) in enumerate(zip(train_loader, valid_loader)):
        train_X, train_y = train_X.to(device, non_blocking=True), train_y.to(device, non_blocking=True)
        valid_X, valid_y = valid_X.to(device, non_blocking=True), valid_y.to(device, non_blocking=True)
        N = train_X.size(0)

        # arch step (alpha training)
        alpha_optim.zero_grad()
        arch.unrolled_backward(train_X, train_y, valid_X, valid_y, lr, w_optim)
        alpha_optim.step()

        # child network step (w)
        w_optim.zero_grad()
        logits = model(train_X)
        loss = model.criterion(logits, train_y)
        loss.backward()

        # gradient clipping
        nn.utils.clip_grad_norm_(model.weights(), config.w_grad_clip)
        w_optim.step()

        prec1, prec5 = utils.accuracy(logits, train_y, topk=(1, 5))
        losses.update(loss.item(), N)
        top1.update(prec1.item(), N)
        top5.update(prec5.item(), N)

        if step % config.print_freq == 0 or step == len(train_loader) - 1:
            print("\r", end="", flush=True)
            logger.info(
                "Train: [{:2d}/{}] Step {:03d}/{:03d} Loss {losses.avg:.3f} "
                "Prec@(1,5) ({top1.avg:.1%}, {top5.avg:.1%})".format(epoch + 1, config.epochs, step,
                                                                     len(train_loader) - 1, losses=losses,
                                                                     top1=top1, top5=top5))
        else:
            print("\rTrain: [{:2d}/{}] Step {:03d}/{:03d} Loss {losses.avg:.3f} "
                  "Prec@(1,5) ({top1.avg:.1%}, {top5.avg:.1%})".format(epoch + 1, config.epochs, step,
                                                                       len(train_loader) - 1, losses=losses,
                                                                       top1=top1, top5=top5), end="", flush=True)

        tb_writer.add_scalar('train/loss', loss.item(), cur_step)
        tb_writer.add_scalar('train/top1', prec1.item(), cur_step)
        tb_writer.add_scalar('train/top5', prec5.item(), cur_step)

        if step % (config.print_freq // 5) == 0 or step == len(train_loader) - 1:  # not too much logging
            for i, tensor in enumerate(model.alpha_normal):
                for j, lsn in enumerate(F.softmax(tensor, dim=-1)):
                    tb_writer.add_scalars('alpha_normal/%d ~~ %d' % ((j - 2), i),
                                          {'max_pl3': lsn[0], 'avg_pl3': lsn[1], 'skip_cn': lsn[2], 'sep_conv3': lsn[3],
                                           'sep_conv5': lsn[4], 'dil_conv3': lsn[5], 'dil_conv5': lsn[6],
                                           'none': lsn[7]}, cur_step)
            for i, tensor in enumerate(model.alpha_reduce):
                for j, lsr in enumerate(F.softmax(tensor, dim=-1)):
                    tb_writer.add_scalars('alpha_reduce/%d ~~ %d' % ((j - 2), i),
                                          {'max_pl3': lsr[0], 'avg_pl3': lsr[1], 'skip_cn': lsr[2], 'sep_conv3': lsr[3],
                                           'sep_conv5': lsr[4], 'dil_conv3': lsr[5], 'dil_conv5': lsr[6],
                                           'none': lsr[7]}, cur_step)

        cur_step += 1

    logger.info("Train: [{:2d}/{}] Final Prec@1 {:.4%}".format(epoch + 1, config.epochs, top1.avg))


def validate(valid_loader, model, epoch, cur_step):
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()
    losses = utils.AverageMeter()

    model.eval()

    with torch.no_grad():
        for step, (X, y) in enumerate(valid_loader):
            X, y = X.to(device, non_blocking=True), y.to(device, non_blocking=True)
            N = X.size(0)

            logits = model(X)
            loss = model.criterion(logits, y)

            prec1, prec5 = utils.accuracy(logits, y, topk=(1, 5))
            losses.update(loss.item(), N)
            top1.update(prec1.item(), N)
            top5.update(prec5.item(), N)

            if step % config.print_freq == 0 or step == len(valid_loader) - 1:
                print("\r", end="", flush=True)
                logger.info(
                    "Valid: [{:2d}/{}] Step {:03d}/{:03d} Loss {losses.avg:.3f} "
                    "Prec@(1,5) ({top1.avg:.1%}, {top5.avg:.1%})".format(epoch + 1, config.epochs, step,
                                                                         len(valid_loader) - 1, losses=losses,
                                                                         top1=top1, top5=top5))
            else:
                print("\rValid: [{:2d}/{}] Step {:03d}/{:03d} Loss {losses.avg:.3f} "
                      "Prec@(1,5) ({top1.avg:.1%}, {top5.avg:.1%})".format(epoch + 1, config.epochs, step,
                                                                           len(valid_loader) - 1, losses=losses,
                                                                           top1=top1, top5=top5), end="", flush=True)
    tb_writer.add_scalar('val/loss', losses.avg, cur_step)
    tb_writer.add_scalar('val/top1', top1.avg, cur_step)
    tb_writer.add_scalar('val/top5', top5.avg, cur_step)

    logger.info("Valid: [{:2d}/{}] Final Prec@1 {:.4%}".format(epoch + 1, config.epochs, top1.avg))

    return top1.avg


if __name__ == "__main__":
    main()
