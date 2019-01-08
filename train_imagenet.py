import argparse
import os
import shutil
import time

import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.datasets as datasets
import torchvision.transforms as transforms
from tensorboardX import SummaryWriter
import logging

import models
from imagenet import config as config, utils as utils
from modules import ABN, SingleGPU
from dataset.sampler import TestDistributedSampler
from functools import partial

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('config', metavar='CONFIG_FILE',
                    help='path to configuration file')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--local_rank', default=0, type=int,
                    help='process rank on node')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--log-dir', type=str, default='.', metavar='PATH',
                    help='output directory for Tensorboard log')
parser.add_argument('--log-hist', action='store_true',
                    help='log histograms of the weights')

best_prec1 = 0
args = None
conf = None
tb = None
logger = None

def init_logger(rank, log_dir):
    global logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(os.path.join(log_dir, 'training_{}.log'.format(rank)))
    formatter = logging.Formatter("%(asctime)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if rank == 0:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

def main():
    global args, best_prec1, logger, conf, tb
    args = parser.parse_args()

    torch.cuda.set_device(args.local_rank)

    try:
      world_size = int(os.environ['WORLD_SIZE'])
      distributed = world_size > 1
    except:
      distributed = False
      world_size = 1


    if distributed:
        dist.init_process_group(backend=args.dist_backend, init_method='env://')

    rank = 0 if not distributed else dist.get_rank()
    init_logger(rank, args.log_dir)
    tb = SummaryWriter(args.log_dir) if rank == 0 else None



    # Load configuration
    conf = config.load_config(args.config)

    # Create model
    model_params = utils.get_model_params(conf["network"])
    model = models.__dict__["net_" + conf["network"]["arch"]](**model_params)

    model.cuda()
    if distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                 output_device=args.local_rank)
    else:
        model = SingleGPU(model)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()
    optimizer, scheduler = utils.create_optimizer(conf["optimizer"], model)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            logger.info("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            logger.warning("=> no checkpoint found at '{}'".format(args.resume))
    else:
        init_weights(model)
        args.start_epoch = 0

    cudnn.benchmark = True

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'val')

    train_transforms, val_transforms = utils.create_transforms(conf["input"])
    train_dataset = datasets.ImageFolder(traindir, transforms.Compose(train_transforms))

    if distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    else:
        train_sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=conf["optimizer"]["batch_size"]//world_size, shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)

    val_dataset = datasets.ImageFolder(valdir, transforms.Compose(val_transforms))
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=conf["optimizer"]["batch_size"]//world_size, shuffle=False,
        num_workers=args.workers, pin_memory=True, sampler = TestDistributedSampler(val_dataset))

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    for epoch in range(args.start_epoch, conf["optimizer"]["schedule"]["epochs"]):
        if distributed:
            train_sampler.set_epoch(epoch)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, scheduler, epoch)

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion, it=epoch * len(train_loader))

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        save_checkpoint({
            'epoch': epoch + 1,
            'arch': conf["network"]["arch"],
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
            'optimizer': optimizer.state_dict(),
        }, is_best)


def train(train_loader, model, criterion, optimizer, scheduler, epoch):
    global logger, conf, tb
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    if conf["optimizer"]["schedule"]["mode"] == "epoch":
        scheduler.step(epoch)

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        if conf["optimizer"]["schedule"]["mode"] == "step":
            scheduler.step(i + epoch * len(train_loader))

        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(non_blocking=True)

        # compute output
        output = model(input)
        loss = criterion(output, target)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        if conf["optimizer"]["clip"] != 0.:
            nn.utils.clip_grad_norm(model.parameters(), conf["optimizer"]["clip"])
        optimizer.step()

        # measure accuracy and record loss
        with torch.no_grad():
          output = output.detach()
          loss = loss.detach()*target.shape[0]
          prec1, prec5 = accuracy_sum(output, target, topk=(1, 5))
          count = target.new_tensor([target.shape[0]],dtype=torch.long)
          if dist.is_initialized():
            dist.all_reduce(count, dist.ReduceOp.SUM)
          for meter,val in (losses,loss), (top1,prec1), (top5,prec5):
            if dist.is_initialized():
              dist.all_reduce(val, dist.ReduceOp.SUM)
            val /= count.item()
            meter.update(val.item(), count.item())


        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            logger.info('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) \t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f}) \t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f}) \t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f}) \t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses, top1=top1, top5=top5))
      
        if not dist.is_initialized() or dist.get_rank() == 0:
          tb.add_scalar("train/loss", losses.val, i + epoch * len(train_loader))
          tb.add_scalar("train/lr", scheduler.get_lr()[0], i + epoch * len(train_loader))
          tb.add_scalar("train/top1", top1.val, i + epoch * len(train_loader))
          tb.add_scalar("train/top5", top5.val, i + epoch * len(train_loader))
          if args.log_hist and i % 10 == 0:
            for name, param in model.named_parameters():
                if name.find("fc") != -1 or name.find("bn_out") != -1:
                    tb.add_histogram(name, param.clone().cpu().data.numpy(), i + epoch * len(train_loader))


def validate(val_loader, model, criterion, it=None):
    global logger, tb
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()

    rank = dist.get_rank() if dist.is_initialized() else 0
    world_size = dist.get_world_size() if dist.is_initialized() else 1

    def process(input, target, all_reduce=None):
        with torch.no_grad():

            target = target.cuda(non_blocking=True)

            # compute output
            output = model(input)
            loss = criterion(output, target)

            # measure accuracy and record loss
            prec1, prec5 = accuracy_sum(output.data, target, topk=(1, 5))

            loss *= target.shape[0]
            count = target.new_tensor([target.shape[0]],dtype=torch.long)
            if all_reduce:
              all_reduce(count)
            for meter,val in (losses,loss), (top1,prec1), (top5,prec5):
              if all_reduce:
                all_reduce(val)
              val /= count.item()
              meter.update(val.item(), count.item())

    # deal with remainder
    all_reduce = partial(dist.all_reduce, op=dist.ReduceOp.SUM) if dist.is_initialized() else None
    last_group_size = len(val_loader.dataset) % world_size
    for i, (input, target) in enumerate(val_loader):
      if input.shape[0] > 1 or last_group_size == 0:
        process(input, target, all_reduce)
      else:
        process(input, target, partial(dist.all_reduce, op=dist.ReduceOp.SUM, group=dist.new_group(range(last_group_size))))

      # measure elapsed time
      batch_time.update(time.time() - end)
      end = time.time()

      if i % args.print_freq == 0:
         logger.info('Test: [{0}/{1}] \t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) \t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f}) \t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f}) \t'
                      'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                    i, len(val_loader), batch_time=batch_time, loss=losses,
                    top1=top1, top5=top5))

    if input.shape[0]==1 and rank > last_group_size > 0:
      dist.new_group(range(last_group_size))

    logger.info(' * Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f}'
          .format(top1=top1, top5=top5))

    if it is not None and (not dist.is_initialized() or dist.get_rank() == 0):
        tb.add_scalar("val/loss", losses.avg, it)
        tb.add_scalar("val/top1", top1.avg, it)
        tb.add_scalar("val/top5", top5.avg, it)

    return top1.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy_sum(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0))
    return res


def init_weights(model):
    global conf
    for name, m in model.named_modules():
        if isinstance(m, nn.Conv2d):
            init_fn = getattr(nn.init, conf["network"]["weight_init"]+'_')
            if conf["network"]["weight_init"].startswith("xavier") or conf["network"]["weight_init"] == "orthogonal":
                gain = conf["network"]["weight_gain_multiplier"]
                if conf["network"]["activation"] == "relu" or conf["network"]["activation"] == "elu":
                    gain *= nn.init.calculate_gain("relu")
                elif conf["network"]["activation"] == "leaky_relu":
                    gain *= nn.init.calculate_gain("leaky_relu", conf["network"]["leaky_relu_slope"])
                init_fn(m.weight, gain)
            elif conf["network"]["weight_init"].startswith("kaiming"):
                if conf["network"]["activation"] == "relu" or conf["network"]["activation"] == "elu":
                    init_fn(m.weight, 0)
                else:
                    init_fn(m.weight, conf["network"]["leaky_relu_slope"])

            if hasattr(m, "bias") and m.bias is not None:
                nn.init.constant_(m.bias, 0.)
        elif isinstance(m, nn.BatchNorm2d) or isinstance(m, ABN):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)
        elif isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight, .1)
            nn.init.constant_(m.bias, 0.)


if __name__ == '__main__':
    main()
