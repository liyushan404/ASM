"""Tune-integrated training script for parallel experiments."""
import requests
import argparse
import collections
import gc
import glob
import os
import pickle
import pprint
import time
import random
import shutil

import numpy as np
import pandas as pd
import ray
from ray import tune
import psutil
from ray.tune import logger as tune_logger
import torch
import torch.nn as nn
import torch.nn.functional as F
from ray.tune.schedulers import ASHAScheduler
from torch.utils import data

import common
import datasets
import estimators as estimators_lib
from estimators import use_ctypes
import experiments
import fair_sampler
import join_utils
import made
import train_utils
import utils

import datetime
import traceback

from torch.autograd import Variable
import itertools

import subprocess as sp

available_dataset = datasets.dataset_list

def auto_garbage_collect(pct=80.0):
    if psutil.virtual_memory().percent >= pct:
        gc.collect()
    return

class DataParallelPassthrough(torch.nn.DataParallel):
    """Wraps a model with nn.DataParallel and provides attribute accesses."""

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.module, name)


def PrintGrads(named_parameters):
    for n, p in named_parameters:
        if p.grad is None:
            continue
        print('param', n, 'grad', p.grad.data)
        assert not torch.any(torch.isnan(p.grad))

def TotalGradNorm(parameters, norm_type=2):
    total_norm = 0
    for p in parameters:
        if p.grad is None:
            continue
        param_norm = p.grad.data.norm(norm_type)
        total_norm += param_norm.item()**norm_type
    total_norm = total_norm**(1. / norm_type)
    return total_norm


def run_epoch(split,
              model,
              opt,
              train_data,
              val_data=None,
              batch_size=100,
              upto=None,
              epoch_num=None,
              epochs=1,
              verbose=False,
              log_every=10,
              return_losses=False,
              table_bits=None,
              warmups=1000,
              loader=None,
              constant_lr=None,
              use_meters=True,
              summary_writer=None,
              lr_scheduler=None,
              custom_lr_lambda=None,
              label_smoothing=0.0,
              neurocard_instance=None,
              num_sample_per_tuple=1
              ):
    if neurocard_instance is not None :
        accum_iter = neurocard_instance.accum_iter
        max_step = neurocard_instance.max_steps
    else:
        accum_iter = 1
    torch.set_grad_enabled(split == 'train')
    model.train() if split == 'train' else model.eval()
    dataset = train_data if split == 'train' else val_data
    losses = []

    if loader is None:
        loader = data.DataLoader(dataset,
                                 batch_size=batch_size,
                                 shuffle=(split == 'train'))

    # How many orderings to run for the same batch?
    nsamples = 1
    if hasattr(model, 'orderings'):
        nsamples = len(model.orderings)
        if verbose:
            print('setting nsamples to', nsamples)

    dur_meter = train_utils.AverageMeter(
        'dur', lambda v: '{:.0f}s'.format(v), display_average=False)
    lr_meter = train_utils.AverageMeter('lr', ':.5f', display_average=False)
    tups_meter = train_utils.AverageMeter('tups',
                                          utils.HumanFormat,
                                          display_average=False)
    loss_meter = train_utils.AverageMeter('loss (bits/tup)', ':.2f')
    train_throughput = train_utils.AverageMeter('tups/s',
                                                utils.HumanFormat,
                                                display_average=False)
    batch_time = train_utils.AverageMeter('sgd_ms', ':3.1f')
    data_time = train_utils.AverageMeter('data_ms', ':3.1f')
    progress = train_utils.ProgressMeter(upto, [
        batch_time,
        data_time,
        dur_meter,
        lr_meter,
        tups_meter,
        train_throughput,
        loss_meter,
    ])

    begin_time = t1 = time.time()

    for step, xb in enumerate(loader):
        data_time.update((time.time() - t1) * 1e3)
        if split == 'train':
            if isinstance(dataset, data.IterableDataset):
                global_steps = upto * epoch_num + step + 1
            else:
                global_steps = len(loader) * epoch_num + step + 1

            if constant_lr:
                lr = constant_lr
                for param_group in opt.param_groups:
                    param_group['lr'] = lr
            elif custom_lr_lambda:
                lr_scheduler = None
                lr = custom_lr_lambda(global_steps)
                for param_group in opt.param_groups:
                    param_group['lr'] = lr
            elif lr_scheduler is None:
                t = warmups
                if warmups < 1:  # A ratio.
                    t = int(warmups * upto * epochs)

                d_model = model.embed_size
                lr = (d_model**-0.5) * min(
                    (global_steps**-.5), global_steps * (t**-1.5))
                for param_group in opt.param_groups:
                    param_group['lr'] = lr
            else:
                # We'll call lr_scheduler.step() below.
                lr = opt.param_groups[0]['lr']

        if upto and step >= upto:
            break

        if isinstance(xb, list):
            # This happens if using data.TensorDataset.
            assert len(xb) == 1, xb
            xb = xb[0]

        xb = xb.float().to(train_utils.get_device(), non_blocking=True)

        # Forward pass, potentially through several orderings.
        xbhat = None
        model_logits = []
        num_orders_to_forward = 1
        if split == 'test' and nsamples > 1:
            # At test, we want to test the 'true' nll under all orderings.
            num_orders_to_forward = nsamples

        for i in range(num_orders_to_forward):
            if hasattr(model, 'update_masks'):
                # We want to update_masks even for first ever batch.
                model.update_masks()

            model_out = model(xb)
            model_logits.append(model_out)
            if xbhat is None:
                xbhat = torch.zeros_like(model_out)
            xbhat += model_out

        if num_orders_to_forward == 1:
            loss = model.nll(xbhat, xb, label_smoothing=label_smoothing).mean()
        else:
            assert False
            # Average across orderings & then across minibatch.
            #
            #   p(x) = 1/N sum_i p_i(x)
            #   log(p(x)) = log(1/N) + log(sum_i p_i(x))
            #             = log(1/N) + logsumexp ( log p_i(x) )
            #             = log(1/N) + logsumexp ( - nll_i (x) )
            #
            # Used only at test time.
            logps = []  # [batch size, num orders]
            assert len(model_logits) == num_orders_to_forward, len(model_logits)
            for logits in model_logits:
                # Note the minus.
                logps.append(
                    -model.nll(logits, xb, label_smoothing=label_smoothing))
            logps = torch.stack(logps, dim=1)
            logps = logps.logsumexp(dim=1) + torch.log(
                torch.tensor(1.0 / nsamples, device=logps.device))
            loss = (-logps).mean()

        losses.append(loss.detach().item())

        if split == 'train':
            if step == 0:
                opt.zero_grad()

            loss = loss / accum_iter
            loss.backward()
            l2_grad_norm = TotalGradNorm(model.parameters())
            if ((step + 1) % accum_iter == 0) or (step + 1 == max_step):
                opt.step()
                opt.zero_grad()
                if lr_scheduler is not None:
                    lr_scheduler.step()

            loss_bits = loss.item() / np.log(2)

            # Number of tuples processed in this epoch so far.
            ntuples = (step + 1) * batch_size
            if use_meters:
                dur = time.time() - begin_time
                lr_meter.update(lr)
                tups_meter.update(ntuples)
                loss_meter.update(loss_bits)
                dur_meter.update(dur)
                train_throughput.update(ntuples / dur)

            if summary_writer is not None:
                # wandb.log({
                #     'train/lr': lr,
                #     'train/tups': ntuples,
                #     'train/tups_per_sec': ntuples / dur,
                #     'train/nll': loss_bits,
                #     'train/global_step': global_steps,
                #     'train/l2_grad_norm': l2_grad_norm,
                # })
                summary_writer.add_scalar('train/lr',
                                          lr,
                                          global_step=global_steps)
                summary_writer.add_scalar('train/tups',
                                          ntuples,
                                          global_step=global_steps)
                summary_writer.add_scalar('train/tups_per_sec',
                                          ntuples / dur,
                                          global_step=global_steps)
                summary_writer.add_scalar('train/nll',
                                          loss_bits,
                                          global_step=global_steps)

            if step % log_every == 0:
                if table_bits:
                    print(
                        'Epoch {} Iter {}, {} entropy gap {:.4f} bits (loss {:.3f}, data {:.3f}) {:.5f} lr, {} tuples seen ({} tup/s)'
                        .format(
                            epoch_num, step, split,
                            loss.item() / np.log(2) - table_bits,
                            loss.item() / np.log(2), table_bits, lr,
                            utils.HumanFormat(ntuples),
                            utils.HumanFormat(ntuples /
                                              (time.time() - begin_time))))
                elif not use_meters:
                    print(
                        'Epoch {} Iter {}, {} loss {:.3f} bits/tuple, {:.5f} lr'
                        .format(epoch_num, step, split,
                                loss.item() / np.log(2), lr))

        if verbose:
            print('%s epoch average loss: %f' % (split, np.mean(losses)))

        batch_time.update((time.time() - t1) * 1e3)
        t1 = time.time()
        if split == 'train' and step % log_every == 0 and use_meters:
            progress.display(step)

    if neurocard_instance is not None:
        neurocard_instance.lloss = loss
    if return_losses:
        return losses
    return np.mean(losses)


def MakeMade(
        table,
        scale,
        layers,
        cols_to_train,
        seed,
        factor_table=None,
        fixed_ordering=None,
        special_orders=0,
        order_content_only=True,
        order_indicators_at_front=True,
        inv_order=True,
        residual=True,
        direct_io=True,
        input_encoding='embed',
        output_encoding='embed',
        embed_size=32,
        dropout=True,
        grouped_dropout=False,
        subvar_dropout=False,
        adjust_fact_col=False,
        per_row_dropout=False,
        fixed_dropout_ratio=False,
        input_no_emb_if_leq=False,
        embs_tied=True,
        resmade_drop_prob=0.,
        # Join specific:
        num_joined_tables=None,
        table_dropout=None,
        table_num_columns=None,
        table_column_types=None,
        table_indexes=None,
        table_primary_index=None,
        # DMoL
        num_dmol=0,
        scale_input=False,
        dmol_cols=[],
        activation=nn.ReLU,
        # Continuous
        learnable_unk=True
):
    dmol_col_indexes = []
    for i in range(len(cols_to_train)):
        dmol_col_indexes.append(i)
    model = made.MADE(
        nin=len(cols_to_train),
        hidden_sizes=[scale] *
        layers if layers > 0 else [512, 256, 512, 128, 1024],
        nout=sum([c.DistributionSize() for c in cols_to_train]),
        num_masks=max(1, special_orders),
        natural_ordering=True,
        input_bins=[c.DistributionSize() for c in cols_to_train],
        do_direct_io_connections=direct_io,
        input_encoding=input_encoding,
        output_encoding=output_encoding,
        embed_size=embed_size,
        input_no_emb_if_leq=input_no_emb_if_leq,
        embs_tied=embs_tied,
        residual_connections=residual,
        factor_table=factor_table,
        seed=seed,
        fixed_ordering=fixed_ordering,
        resmade_drop_prob=resmade_drop_prob,

        # Wildcard skipping:
        dropout_p=dropout,
        fixed_dropout_p=fixed_dropout_ratio,
        grouped_dropout=grouped_dropout,
        subvar_dropout=subvar_dropout,
        adjust_fact_col=adjust_fact_col,
        learnable_unk=learnable_unk,
        per_row_dropout=per_row_dropout,

        # DMoL
        num_dmol=num_dmol,
        scale_input=scale_input,
        dmol_col_indexes=dmol_col_indexes,

        # Join support.
        num_joined_tables=num_joined_tables,
        table_dropout=table_dropout,
        table_num_columns=table_num_columns,
        table_column_types=table_column_types,
        table_indexes=table_indexes,
        table_primary_index=table_primary_index,

        activation=activation
    ).to(train_utils.get_device())

    if special_orders > 0:
        orders = []

        if order_content_only:
            print('Leaving out virtual columns from orderings')
            cols = [c for c in cols_to_train if not c.name.startswith('__')]
            inds_cols = [c for c in cols_to_train if c.name.startswith('__in_')]
            num_indicators = len(inds_cols)
            num_content, num_virtual = len(cols), len(cols_to_train) - len(cols)

            # Data: { content }, { indicators }, { fanouts }.
            for i in range(special_orders):
                rng = np.random.RandomState(i + 1)
                content = rng.permutation(np.arange(num_content))
                inds = rng.permutation(
                    np.arange(num_content, num_content + num_indicators))
                fanouts = rng.permutation(
                    np.arange(num_content + num_indicators, len(cols_to_train)))

                if order_indicators_at_front:
                    # Model: { indicators }, { content }, { fanouts },
                    # permute each bracket independently.
                    order = np.concatenate(
                        (inds, content, fanouts)).reshape(-1,)
                else:
                    # Model: { content }, { indicators }, { fanouts }.
                    # permute each bracket independently.
                    order = np.concatenate(
                        (content, inds, fanouts)).reshape(-1,)
                assert len(np.unique(order)) == len(cols_to_train), order
                orders.append(order)
        else:
            # Permute content & virtual columns together.
            for i in range(special_orders):
                orders.append(
                    np.random.RandomState(i + 1).permutation(
                        np.arange(len(cols_to_train))))

        if factor_table:
            # Correct for subvar ordering.
            for i in range(special_orders):
                # This could have [..., 6, ..., 4, ..., 5, ...].
                # So we map them back into:
                # This could have [..., 4, 5, 6, ...].
                # Subvars have to be in order and also consecutive
                order = orders[i]
                for orig_col, sub_cols in factor_table.fact_col_mapping.items():
                    first_subvar_index = cols_to_train.index(sub_cols[0])
                    print('Before', order)
                    for j in range(1, len(sub_cols)):
                        subvar_index = cols_to_train.index(sub_cols[j])
                        order = np.delete(order,
                                          np.argwhere(order == subvar_index))
                        order = np.insert(
                            order,
                            np.argwhere(order == first_subvar_index)[0][0] + j,
                            subvar_index)
                    orders[i] = order
                    print('After', order)

        print('Special orders', np.array(orders))

        if inv_order:
            for i, order in enumerate(orders):
                orders[i] = np.asarray(utils.InvertOrder(order))
            print('Inverted special orders:', orders)

        model.orderings = orders
    return model


class NeuroCard(tune.Trainable):
    def setup(self, config):
        self.config = config
        torch.cuda.empty_cache()
        print('NeuroCard config:')
        pprint.pprint(config)

        os.chdir(config['cwd'])
        for k, v in config.items():
            setattr(self, k, v)
        if hasattr(self,'join_pred'):
            os.environ['join_pred'] = self.join_pred

        if hasattr(self,'distinct_fanout_col'):
            print("distinct_fanout_col option ON")
            os.environ['distinct_fanout_col'] = self.distinct_fanout_col

        if config['__gpu'] == 0:
            torch.set_num_threads(config['__cpu'])

        self.total_train_time = 0
        self.mininum_loss = -1
        # W&B.
        # Do wandb.init() after the os.chdir() above makes sure that the Git
        # diff file (diff.patch) is w.r.t. the directory where this file is in,
        # rather than w.r.t. Ray's package dir.
        # wandb_project = config['__run']
        # now = datetime.datetime.now().strftime('%Y%m%d-%H:%M:%S:%f')
        # wandb_name = f"trial_{workload_name}_{self.fc_hiddens}_{self.embed_size}_{self.word_size_bits}_{self.layers}.tar'"
        # wandb.init(name=wandb_name,
        #            sync_tensorboard=True,
        #            config=config,
        #            project=wandb_project)

        if ('epoch' not in dir(self)) or (self.epoch is None) :
            self.epoch = 0
        if 'random_seed' not in dir(self) :
            self.random_seed = None
        if self.random_seed is not None :
            torch.manual_seed(self.random_seed)
            np.random.seed(self.random_seed)
            self.rng = np.random.RandomState(self.random_seed)
        else : self.rng = None

        # if len(model_path_list)>0:
        #     last_epoch = 0
        #     for path in model_path_list:
        #         model_epoch = int(path.split('_')[1])
        #         if last_epoch < model_epoch:
        #             last_epoch = model_epoch
        #             self.checkpoint_to_load = path

        if isinstance(self.join_tables, int):
            # Hack to support training single-model tables.
            sorted_table_names = sorted(
                list(datasets.JoinOrderBenchmark.GetJobLightJoinKeys().keys()))
            self.join_tables = [sorted_table_names[self.join_tables]]

        # Try to make all the runs the same, except for input orderings.


        # Common attributes.
        self.loader = None
        self.join_spec = None
        join_iter_dataset = None
        table_primary_index = None

        if self.PK_tuples_np_loc is not None:
            import pickle5
            PK_tuples_np = pickle5.load(open(self.PK_tuples_np_loc, "rb"))
        else:
            assert False
            PK_tuples_np = None

        # New datasets should be loaded here.
        assert self.dataset in available_dataset # +@ assert available dataset, not only imdb

        print('Training on Join({})'.format(self.join_tables))
        loaded_tables = []
        for t in self.join_tables:
            print('Loading', t)
            table = datasets.LoadDataset(self.dataset,
                                         t,
                                         data_dir=self.data_dir,
                                         use_cols=self.use_cols,
                                         pre_add_noise=self.pre_add_noise,
                                         pre_normalize=self.pre_normalize,
                                         post_normalize=self.post_normalize,
                                         #irr_attrs=self.irr_attrs
                                         PK_tuples_np=PK_tuples_np
                                         )
            table.data.info()
            loaded_tables.append(table)

        if len(self.join_tables) > 1:
            join_spec, join_iter_dataset, loader, table = self.MakeSamplerDatasetLoader(loaded_tables)
            self.join_spec = join_spec
            self.train_data = join_iter_dataset
            self.loader = loader
            table_primary_index = [t.name for t in loaded_tables].index(self.join_spec.join_root)
            if hasattr(join_iter_dataset,'join_iter_dataset') and hasattr(join_iter_dataset.join_iter_dataset,'sampler'):
                table.cardinality = join_iter_dataset.join_iter_dataset.sampler.join_card
                print(f"True cardinality - from jct  {table.cardinality}")
            elif hasattr(join_iter_dataset,'sampler'):
                table.cardinality = join_iter_dataset.sampler.join_card
                print(f"True cardinality - from jct  {table.cardinality}")
            else:
                assert False
                table.cardinality = datasets.get_cardinality(self.dataset,self.join_tables)
                print(f"True cardinality - {table.cardinality}")
            # table.cardinality = datasets.get_cardinality(self.dataset,self.join_tables)
            self.train_data.cardinality = table.cardinality

        else:
            # Train on a single table.
            table = loaded_tables[0]
            print(f"True cardinality - single table {table.cardinality}")

        print('Table loading done')


        if len(self.join_tables) == 1:
            join_spec = join_utils.get_single_join_spec(self.__dict__)
            self.join_spec = join_spec
            table.data.info()

            self.join_keys = list(PK_tuples_np.keys())
            self.train_data = self.MakeTableDataset(table,
                                                    add_noise=self.post_add_noise,
                                                    join_keys=self.join_keys,
                                                    subvar_dropout=self.subvar_dropout,
                                                    adjust_fact_col=self.adjust_fact_col)
        else:
            assert False

        print('Training data loading done')

        self.table = table
        # Provide true cardinalities in a file or implement an oracle CardEst.
        self.oracle = None
        self.table_bits = 0

        self.fixed_ordering = self.MakeOrdering(table)

        if self.activation == 'tanh':
            self.activation = nn.Tanh
            assert False
        else:
            self.activation = nn.ReLU

        self.dmol_cols = []

        model = self.MakeModel(self.table,
                               self.train_data,
                               table_primary_index=table_primary_index)

        # NOTE: ReportModel()'s returned value is the true model size in
        # megabytes containing all all *trainable* parameters.  As impl
        # convenience, the saved ckpts on disk have slightly bigger footprint
        # due to saving non-trainable constants (the masks in each layer) as
        # well.  They can be deterministically reconstructed based on RNG seeds
        # and so should not be counted as model size.
        self.mb = train_utils.ReportModel(model)
        model.apply(train_utils.weight_init)
        self.model = model

        if self.use_data_parallel:
            self.model = DataParallelPassthrough(self.model)

        if self.optimizer == 'adam':
            opt = torch.optim.Adam(list(model.parameters()), 2e-4)
        else:
            print('Using Adagrad')
            opt = torch.optim.Adagrad(list(model.parameters()), 2e-4)
        self.opt = opt
        total_steps = self.epochs * self.max_steps
        if self.lr_scheduler == 'CosineAnnealingLR':
            # Starts decaying to 0 immediately.
            self.lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                opt,total_steps)#total_steps
        elif self.lr_scheduler == 'OneCycleLR':
            # Warms up to max_lr, then decays to ~0.
            self.lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
                opt, max_lr=2e-3, total_steps=total_steps)
        elif self.lr_scheduler is not None and self.lr_scheduler.startswith(
                'OneCycleLR-'):
            warmup_percentage = float(self.lr_scheduler.split('-')[-1])
            # Warms up to max_lr, then decays to ~0.
            self.lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
                opt,
                max_lr=2e-3,
                total_steps=total_steps,
                pct_start=warmup_percentage)
        elif self.lr_scheduler is not None and self.lr_scheduler.startswith(
                'wd_'):
            # Warmups and decays.
            splits = self.lr_scheduler.split('_')
            assert len(splits) == 3, splits
            lr, warmup_fraction = float(splits[1]), float(splits[2])
            self.custom_lr_lambda = train_utils.get_cosine_learning_rate_fn(
                total_steps,
                learning_rate=lr,
                min_learning_rate_mult=1e-5,
                constant_fraction=0.,
                warmup_fraction=warmup_fraction)
        else:
            assert self.lr_scheduler is None, self.lr_scheduler

        self.tbx_logger = tune_logger.TBXLogger(self.config, self.logdir)
        if self.checkpoint_to_load is not None:
            self.LoadCheckpoint()
        else:
            assert False


        self.loaded_queries = None
        self.oracle_cards = None
        not_support_query_idx = list()
        if self.dataset in available_dataset: #and len(self.join_tables) > 1:
            if self.queries_csv is not None:
                queries_job_format, not_support_query_idx = utils.FormattingQuery_JoinFilter(self.queries_csv, self.join_clauses,sep=self.sep, dataset=self.dataset, use_cols=self.use_cols)
                self.loaded_queries, self.oracle_cards = utils.UnpackQueries(
                    self.table, queries_job_format, not_support_query_idx)
        self.not_support_query_idx = not_support_query_idx
        if config['__gpu'] == 0:
            print('CUDA not available, using # cpu cores for intra-op:',
                  torch.get_num_threads(), '; inter-op:',
                  torch.get_num_interop_threads())

        if not self.use_data_parallel:
            print("DO NOT USE MULTI GPU")

        if hasattr(self,'update_dir')  and self.update_dir is not None:
            table.cardinality = self.UpdateSampler(self.join_tables,self.update_dir)
            self.train_data.cardinality = table.cardinality

        print('NeuroCard setup done!')

    def LoadCheckpoint(self):
        all_ckpts = glob.glob(self.checkpoint_to_load)
        msg = f'No ckpt found or use tune.grid_search() for >1 ckpts: {self.checkpoint_to_load}'
        assert len(all_ckpts) == 1, msg
        loaded = torch.load(all_ckpts[0])
        # try:
        if isinstance(self.model, DataParallelPassthrough):
            self.model.module.load_state_dict(loaded['model_state_dict'])
        else:
            self.model.load_state_dict(loaded['model_state_dict'])
        if self.mode == 'INFERENCE' :
            print('Loaded ckpt from', all_ckpts[0])
            return None
        self.epoch = loaded['epoch']
        self.opt.load_state_dict(loaded['optimizer_state_dict'])
        self.lloss = loaded['loss']


        print('Loaded ckpt from', all_ckpts[0])

    def MakeTableDataset(self, table, add_noise=False, join_keys=[], subvar_dropout=False, adjust_fact_col=False):
        train_data = common.TableDataset(table, discretize=(self.num_sigmoids <= 0), add_noise=add_noise)
        if self.factorize:
            train_data = common.FactorizedTable(
                train_data, word_size_bits=self.word_size_bits, join_keys=join_keys,
                subvar_dropout=subvar_dropout,
                adjust_fact_col=adjust_fact_col)
        return train_data

    def MakeOrdering(self, table):
        fixed_ordering = None
        if self.dataset not in available_dataset and self.special_orders <= 1:
            fixed_ordering = list(range(len(table.columns)))

        if self.order is not None:
            print('Using passed-in order:', self.order)
            fixed_ordering = self.order

        if self.order_seed is not None:
            if self.order_seed == 'reverse':
                fixed_ordering = fixed_ordering[::-1]
            else:
                rng = np.random.RandomState(self.order_seed)
                rng.shuffle(fixed_ordering)
            print('Using generated order:', fixed_ordering)
        return fixed_ordering

    def MakeModel(self, table, train_data, table_primary_index=None):
        cols_to_train = table.columns
        if self.factorize:
            cols_to_train = train_data.columns

        fixed_ordering = self.MakeOrdering(table)

        table_num_columns = table_column_types = table_indexes = None
        if isinstance(train_data, (common.SamplerBasedIterDataset,
                                   common.FactorizedSampleFromJoinIterDataset)):
            table_num_columns = train_data.table_num_columns
            table_column_types = train_data.combined_columns_types
            table_indexes = train_data.table_indexes
            print('table_num_columns', table_num_columns)
            print('table_column_types', table_column_types)
            print('table_indexes', table_indexes)
            print('table_primary_index', table_primary_index)

        model = MakeMade(
            table=table,
            scale=self.fc_hiddens,
            layers=self.layers,
            cols_to_train=cols_to_train,
            seed=self.seed,
            factor_table=train_data if self.factorize else None,
            fixed_ordering=fixed_ordering,
            special_orders=self.special_orders,
            order_content_only=self.order_content_only,
            order_indicators_at_front=self.order_indicators_at_front,
            inv_order=True,
            residual=self.residual,
            direct_io=self.direct_io,
            input_encoding=self.input_encoding,
            output_encoding=self.output_encoding,
            embed_size=self.embed_size,
            dropout=self.dropout,
            per_row_dropout=self.per_row_dropout,
            grouped_dropout=self.grouped_dropout
            if self.factorize else False,
            subvar_dropout=self.subvar_dropout,
            adjust_fact_col=self.adjust_fact_col,
            fixed_dropout_ratio=self.fixed_dropout_ratio,
            input_no_emb_if_leq=self.input_no_emb_if_leq,
            embs_tied=self.embs_tied,
            resmade_drop_prob=self.resmade_drop_prob,
            # DMoL:
            num_dmol=self.num_dmol,
            scale_input=self.scale_input if self.num_dmol else False,
            dmol_cols=self.dmol_cols if self.num_dmol else [],
            # Join specific:
            num_joined_tables=len(self.join_tables),
            table_dropout=self.table_dropout,
            table_num_columns=table_num_columns,
            table_column_types=table_column_types,
            table_indexes=table_indexes,
            table_primary_index=table_primary_index,
            activation=self.activation,
            # Continuous
            learnable_unk=self.learnable_unk
        )
        return model

    def MakeProgressiveSamplers(self,
                                model,
                                train_data,
                                do_fanout_scaling=False):
        estimators = []
        dropout = self.dropout or self.per_row_dropout
        #assert not dropout
        for n in self.eval_psamples:
            if self.factorize:
                estimators.append(
                    estimators_lib.FactorizedProgressiveSampling(
                        model,
                        train_data,
                        n,
                        self.join_spec,
                        device=train_utils.get_device(),
                        shortcircuit=dropout,
                        do_fanout_scaling=do_fanout_scaling))
            else:
                estimators.append(
                    estimators_lib.ProgressiveSampling(
                        model,
                        train_data,
                        n,
                        self.join_spec,
                        device=train_utils.get_device(),
                        shortcircuit=dropout,
                        do_fanout_scaling=do_fanout_scaling))
        if self.verbose_mode:
            for est in estimators :
                est.set_verbose_data(len(self.loaded_queries))
        return estimators

    def step(self):
        self.model.model_bits = 0
        results = self.evaluate(self.num_eval_queries_at_checkpoint_load,
                                done=True)
        self._maybe_check_asserts(results, returns=None)
        return {
            'epoch': 0,
            'done': True,
            'results': results,
        }

    def _maybe_check_asserts(self, results, returns):
        if self.asserts:
            # asserts = {key: val, ...} where key either exists in "results"
            # (returned by evaluate()) or "returns", both defined above.
            error = False
            message = []
            for key, max_val in self.asserts.items():
                if key in results:
                    if results[key] >= max_val:
                        error = True
                        message.append(str((key, results[key], max_val)))
                elif returns[key] >= max_val:
                    error = True
                    message.append(str((key, returns[key], max_val)))
            assert not error, '\n'.join(message)

    def stop(self):
        self.tbx_logger.flush()
        self.tbx_logger.close()

    def _log_result(self, results):
        psamples = {}
        # When we run > 1 epoch in one tune "iter", we want TensorBoard x-axis
        # to show our real epoch numbers.
        results['iterations_since_restore'] = results[
            'training_iteration'] = self.epoch
        for k, v in results['results'].items():
            if 'psample' in k:
                psamples[k] = v
        # wandb.log(results)
        self.tbx_logger.on_result(results)
        self.tbx_logger._file_writer.add_custom_scalars_multilinechart(
            map(lambda s: 'ray/tune/results/{}'.format(s), psamples.keys()),
            title='psample')

    def ErrorMetric(self, est_card, card):
        if card == 0 and est_card != 0:
            return est_card
        if card != 0 and est_card == 0:
            return card
        if card == 0 and est_card == 0:
            return 1.0
        return max(est_card / card, card / est_card)


    def Query(self,
              estimators,
              oracle_card=None,
              query=None,
              table=None,
              oracle_est=None):
        assert query is not None
        cols, ops, vals = query
        card = oracle_est.Query(cols, ops,
                                vals) if oracle_card is None else oracle_card
        print('Q(', end='')
        for c, o, v in zip(cols, ops, vals):
            print('{} {} {}, '.format(c.name, o, str(v)), end='')
        print('): ', end='')
        print('\n  actual {} ({:.3f}%) '.format(card,
                                                card / table.cardinality * 100),
              end='')
        # +@
        for est in estimators:
            est_card = est.Query(cols, ops, vals, sample_alg=self.sample_alg)
            err = self.ErrorMetric(est_card, card)
            est.AddError(err, est_card, card)

            print('{} {} {} (err={:.3f}) '.format(str(est), est_card, card, err), end='')
        print()

    def NotSupportQuery(self,estimators,code):
        for est in estimators:
            est_card = est.Query([], [], [],not_support=True)
            est.AddError(code, est_card, code)
        print('Not supported query ')


    def ready_for_evaluate(self, use_raw_table=False):
        self.use_raw_table = use_raw_table

        model = self.model
        if isinstance(model, DataParallelPassthrough):
            model = model.module
        model.eval()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

        self.estimators = self.MakeProgressiveSamplers(
            model,
            self.train_data if self.factorize else self.table,
            do_fanout_scaling=(self.dataset in available_dataset) and (len(self.join_tables) > 1) )
        assert len(self.estimators) == 1

        if use_ctypes:
            for est in self.estimators:
                est.make_bytes_domain(use_raw_table)

    def get_num_rows(self):
        for est in self.estimators:
            return est.cardinality

    def get_all_col_names(self):
        for est in self.estimators:
            return est.get_all_col_names()

    def get_natural_idx(self, key):
        for est in self.estimators:
            return est.get_natural_idx(key)

    def get_domain(self, idx):
        for est in self.estimators:
            return est.get_domain(idx)

    def get_P(self, start_idx, return_idx, sample_size, sample, logits, cols, ops, vals, dom_ops):
        for est in self.estimators:
            return est.get_P(start_idx, return_idx, sample_size, sample, logits, cols, ops, vals, dom_ops, use_raw_table=self.use_raw_table, subvar_dropout=self.subvar_dropout, adjust_fact_col=self.adjust_fact_col)

    def sample_put_input(self, probs, sample, natural_idx):
        assert False
        for est in self.estimators:
            return est.sample_put_input(probs, sample, natural_idx)

    def put_sample(self, data_to_encode, sample, natural_idx):
        if self.use_raw_table:
            #TODO store data_to_encode somewhere so we can use it to compute P(key)
            # for any subsequent key --> for now, we're using independence assumption
            assert sample is None
            return None
        for est in self.estimators:
            return est.put_sample(data_to_encode, sample, natural_idx)

    def get_device(self):
        return train_utils.get_device()

    def evaluate_one_tree(self, query, keys=None, sample_size=2048):

        self.model.model_bits = 0

        for est in self.estimators:
            assert self.sample_alg == 'ps', self.sample_alg
            return est.QueryTree(query, sample_alg=self.sample_alg, keys=keys, sample_size=sample_size, use_raw_table=self.use_raw_table, subvar_dropout=self.subvar_dropout, adjust_fact_col=self.adjust_fact_col)


    def evaluate_one(self, query):
        assert False
        self.model.model_bits = 0
        gc.collect()
        torch.cuda.empty_cache()

        cols, ops, vals = query
        for est in self.estimators:
            assert self.sample_alg == 'ps', self.sample_alg
            return est.Query(cols, ops, vals, sample_alg=self.sample_alg)

    def evaluate(self, num_queries, done, estimators=None):
        model = self.model
        if isinstance(model, DataParallelPassthrough):
            model = model.module
        model.eval()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

        results = {}
        if num_queries:
            if estimators is None:
                estimators = self.MakeProgressiveSamplers(
                    model,
                    self.train_data if self.factorize else self.table,
                    do_fanout_scaling=(self.dataset in available_dataset) and (len(self.join_tables) > 1) )
                if self.eval_join_sampling:  # None or an int.
                    assert False
                    estimators = [
                        estimators_lib.JoinSampling(self.train_data, self.table,
                                                    self.eval_join_sampling)
                    ]

            assert self.loaded_queries is not None
            num_queries = min(len(self.loaded_queries), num_queries)
            err_queries = list()
            for i in range(num_queries):
                gc.collect()
                torch.cuda.empty_cache()
                print('Query {}:'.format(i), end=' ')
                query = self.loaded_queries[i]
                if i in self.not_support_query_idx:
                    assert False
                    not_support_code = -1
                    self.NotSupportQuery(estimators,not_support_code)
                else:
                    try:
                        self.Query(estimators,
                                   oracle_card=None if self.oracle_cards is None else
                                   self.oracle_cards[i],
                                   query=query,
                                   table=self.table,
                                   oracle_est=self.oracle)
                    except Exception as e:
                        err_code = 0
                        self.NotSupportQuery(estimators,err_code)
                        err_queries.append(i)
                        trace_log = traceback.format_exc().replace('\n','  ')
                        err_line = f"err - {i}\t{trace_log}\n"
                        with open(f'{self.workload_name}_err_query_trace.log','at') as writer:
                            writer.write(err_line)
                        gc.collect()
                        torch.cuda.empty_cache()
                        assert False


                if i % 100 == 0:
                    for est in estimators:
                        est.report()
            print(f"inf err queries {err_queries}")
            if len(err_queries) > 0:
                with open(f'{self.workload_name}_err_queries.txt','at') as writer:
                    writer.write(f"err queries {err_queries}\n")
            for i,est in enumerate(estimators):
                results[str(est) + '_max'] = np.max(est.errs)
                results[str(est) + '_p99'] = np.quantile(est.errs, 0.99)
                results[str(est) + '_p95'] = np.quantile(est.errs, 0.95)
                results[str(est) + '_median'] = np.median(est.errs)
                est.report()

                series = pd.Series(est.query_dur_ms)
                print(series.describe())
                series.to_csv(str(est) + '.csv', index=False, header=False)

                #if self.verbose_mode:
                #    est.save_verbose_data(self.workload_name)
                if self.save_eval_result:
                    workload = self.workload_name
                    out_dir = f'../../results/NeuroCard'
                    if not os.path.isdir(out_dir):
                        os.mkdir(out_dir)

                    now = datetime.datetime.now().strftime('%Y%m%d-%H:%M:%S')
                    err_df = pd.DataFrame([est.errs,est.est_cards,est.true_cards,est.query_dur_ms,est.query_prep_ms]).transpose()
                    err_df.to_csv(f'{out_dir}/{workload}.csv',index=False,header=['errs','est_cards','true_cards','query_dur_ms','query_prep_ms'])

        return results
