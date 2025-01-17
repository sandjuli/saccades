import numpy as np
import pandas as pd
import xarray as xr
import matplotlib as mpl
from matplotlib import pyplot as plt
import seaborn as sns
from itertools import product
from scipy.io import savemat

import torch
from torch import nn
from torch.optim import SGD, Adam, AdamW
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau

from utils import Timer


criterion = nn.CrossEntropyLoss()
criterion_noreduce = nn.CrossEntropyLoss(reduction='none')
criterion_mse = nn.MSELoss()
criterion_mse_noreduce = nn.MSELoss(reduction='none')

model_dir = 'models/logpolar'
results_dir = 'results/logpolar'
fig_dir = 'figures/logpolar'

def choose_trainer(model, loaders, test_xarray, config):
    if config.model_type in ['cnn', 'bigcnn', 'mlp', 'unserial', 'map2num_decoder']:
        if 'distract' in config.challenge:
            print('Using FeedForwardTrainerDistract class')
            trainer = FeedForwardTrainerDistract(model, loaders, test_xarray, config)
        else:
            print('Using FeedForwardTrainer class')
            trainer = FeedForwardTrainer(model, loaders, test_xarray, config)
    elif config.model_type == 'gated_mapper':
        trainer = TorchRNNTrainer(model, loaders, test_xarray, config)
    elif 'recurrent_control' in config.model_type:
        if 'distract' in config.challenge:
            trainer = RecurrentTrainerDistract(model, loaders, test_xarray, config)
        else:
            trainer = RecurrentTrainer(model, loaders, test_xarray, config)
    elif 'distract' in config.challenge:
        print('Using TrainerDistract class')
        trainer = TrainerDistract(model, loaders, test_xarray, config)
    else:
        print('Using Trainer class')
        trainer = Trainer(model, loaders, test_xarray, config)
    return trainer

class Trainer():
    def __init__(self, model, loaders, test_xarray, config):
        self.model = model
        self.train_loader, self.test_loaders = loaders
        # self.valid_set = test_xarray['validation']
        # self.OOD_set = test_xarray['OOD']
        self.config = config
        self.current_map_f1 = 0
        # Set up optimizer and scheduler
        if config.opt == 'SGD':
            start_lr = 0.1
            mom = 0.9
            self.optimizer = SGD(model.parameters(), lr=start_lr, momentum=mom, weight_decay=config.wd)
            # scheduler = StepLR(opt, step_size=n_epochs/10, gamma=0.7)
            self.scheduler = StepLR(self.optimizer, step_size=config.n_epochs/20, gamma=0.7)
            # self.scheduler = ReduceLROnPlateau(self.optimizer, 'min', verbose=True, patience=2) # if applied on loss
            # self.scheduler = ReduceLROnPlateau(self.optimizer, 'max', verbose=True, patience=2) # if applied on acc
        elif config.opt == 'Adam':
            # start_lr = 0.01 if config.use_loss == 'num' else 0.001
            start_lr = 0.001
            self.optimizer = AdamW(model.parameters(), weight_decay=config.wd, amsgrad=True, lr=start_lr)
            self.scheduler = StepLR(self.optimizer, step_size=config.n_epochs/20, gamma=0.7)
        else:
            print('Selected optimizer not implemented')
            exit()

        self.shuffle = True

        self.nclasses = self.model.output_size
        if 'unique' in config.challenge:
            max_num = 3
            min_num = 1
            self.ticks = list(range(max_num - min_num + 1))
            self.ticklabels = [str(tick + min_num) for tick in self.ticks]
            self.to_subtract = 1
        else:
            self.ticks = list(range(config.max_num - config.min_num + 1))
            self.ticklabels = [str(tick + config.min_num) for tick in self.ticks]
            self.to_subtract = config.min_num
        if config.save_act:
            # Load image metadata for testsets to be saved with activations
            self.image_metadata = [pd.read_pickle(f'{loader.filename}.pkl') for loader in self.test_loaders]
            # self.image_metadata = [xr.open_dataset(f'{loader.filename}.nc') for loader in self.test_loaders]
    
    def train_network(self):
        config = self.config
        base_name = config.base_name
        device = config.device
        avg_num_objects = config.max_num - ((config.max_num-config.min_num)/2)
        n_locs = config.grid**2
        weight_full = (n_locs - avg_num_objects)/ (avg_num_objects+2) # 9 for 9 locations
        weight_count = (n_locs - avg_num_objects)/ avg_num_objects
        pos_weight_count = torch.ones([n_locs], device=device) * weight_count
        pos_weight_full = torch.ones([n_locs], device=device) * weight_full
        self.criterion_bce_full = nn.BCEWithLogitsLoss(pos_weight=pos_weight_full)
        self.criterion_bce_count = nn.BCEWithLogitsLoss(pos_weight=pos_weight_count)
        self.criterion_bce_full_noreduce = nn.BCEWithLogitsLoss(pos_weight=pos_weight_full, reduction='none')
        self.criterion_bce_count_noreduce = nn.BCEWithLogitsLoss(pos_weight=pos_weight_count, reduction='none')
        n_epochs = config.n_epochs

        train_loss = np.zeros((n_epochs + 1,))
        # train_map_loss = np.zeros((n_epochs,))
        train_count_map_loss = np.zeros((n_epochs + 1,))
        train_dist_map_loss = np.zeros((n_epochs + 1,))
        train_full_map_loss = np.zeros((n_epochs + 1,))
        train_count_num_loss = np.zeros((n_epochs + 1,))
        train_dist_num_loss = np.zeros((n_epochs + 1,))
        train_all_num_loss = np.zeros((n_epochs + 1,))
        train_sh_loss = np.zeros((n_epochs + 1,))
        train_acc_count = np.zeros((n_epochs + 1,))
        train_acc_dist = np.zeros((n_epochs + 1,))
        train_acc_all = np.zeros((n_epochs + 1,))
        train_acc_map = np.zeros((n_epochs + 1,))
        n_test_sets = len(config.test_shapes) * len(config.lum_sets)
        test_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        # test_map_loss = [np.zeros((n_epochs,)) for _ in range(n_test_sets)]
        test_full_map_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_count_map_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_dist_map_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_count_num_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_dist_num_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_all_num_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_sh_loss = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_acc_count = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_acc_map = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_acc_dist = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_acc_all = [np.zeros((n_epochs + 1,)) for _ in range(n_test_sets)]
        test_results = pd.DataFrame()

        ###### ASSESS PERFORMANCE BEFORE TRAINING #####
        ep_tr_loss, ep_tr_num_loss, tr_accuracy, ep_tr_sh_loss, ep_tr_map_loss, _, _, tr_map_acc = self.test(self.train_loader, 0)
        train_count_num_loss[0] = ep_tr_num_loss
        train_acc_count[0] = tr_accuracy
        train_acc_map[0] = tr_map_acc
        train_count_map_loss[0], train_full_map_loss[0] = ep_tr_map_loss
        train_loss[0] = ep_tr_loss  # optimized loss
        train_sh_loss[0] = ep_tr_sh_loss
        shape_lum = product(config.test_shapes, config.lum_sets)
        # for ts, (test_loader, (test_shapes, lums)) in enumerate(zip(self.test_loaders, shape_lum)):
        for ts, test_loader in enumerate(self.test_loaders):
            epoch_te_loss, epoch_te_num_loss, te_accuracy, epoch_te_sh_loss, epoch_te_map_loss, epoch_df, _, te_map_acc = self.test(test_loader, 0)
            epoch_df['train shapes'] = str(config.train_shapes)
            # epoch_df['test shapes'] = str(test_shapes)
            # epoch_df['test lums'] = str(lums)
            epoch_df['test shapes'] = str(test_loader.shapes)
            epoch_df['test lums'] = str(test_loader.lums)
            epoch_df['testset'] = test_loader.testset
            epoch_df['viewing'] = test_loader.viewing
            epoch_df['repetition'] = config.rep
            test_results = pd.concat((test_results, epoch_df), ignore_index=True)
            test_count_num_loss[ts][0] = epoch_te_num_loss
            test_acc_count[ts][0] = te_accuracy
            test_acc_map[ts][0] = te_map_acc
            test_count_map_loss[ts][0], test_full_map_loss[ts][0] = epoch_te_map_loss
            test_loss[ts][0] = epoch_te_loss
            test_sh_loss[ts][0] = epoch_te_sh_loss
        print(f'Before Training:')
        print(f'Train (Count/Dist/All) Num Loss={train_count_num_loss[0]:.4}/{train_dist_num_loss[0]:.4}/{train_all_num_loss[0]:.4} \t Accuracy={train_acc_count[0]:.3}%/{train_acc_dist[0]:.3}%/{train_acc_all[0]:.3}')
        print(f'Train (Count/Dist/All) Map Loss={train_count_map_loss[0]:.4}/{train_dist_map_loss[0]:.4}/{train_full_map_loss[0]:.4}')
        print(f'Test (Count/Dist/All) Num Loss={test_count_num_loss[-1][0]:.4}/{test_dist_num_loss[-1][0]:.4}/{test_all_num_loss[-1][0]:.4} \t Accuracy={test_acc_count[-1][0]:.3}%/{test_acc_dist[-1][0]:.3}%/{test_acc_all[-1][0]:.3}')
        print(f'Test (Count/Dist/All) Map Loss={test_count_map_loss[-1][0]:.4}/{test_dist_map_loss[-1][0]:.4}/{test_full_map_loss[-1][0]:.4}')
        
        savethisep = False
        threshold = 51
        if config.save_act:
            print('Saving untrained activations...')
            self.save_activations(self.model, self.test_loaders, base_name + '_init', config)
        
        tr_accuracy = 0
        for ep in range(1, n_epochs + 1):
            if tr_accuracy > threshold:
                savethisep = True
                threshold += 25 # This will be 51,76, and then 101 so we'll save at 51 and 76
            if config.save_act and savethisep:
                print(f'Saving midway activations at {threshold-26}% accuracy...')
                self.save_activations(self.model, self.test_loaders, f'{base_name}_acc{threshold-26}', config)
                savethisep = False
            epoch_timer = Timer()

            ###### TRAIN ######
            ep_tr_loss, ep_tr_num_loss, tr_accuracy, ep_tr_sh_loss, ep_tr_map_loss, map_acc = self.train(self.train_loader, ep)
            train_count_num_loss[ep] = ep_tr_num_loss
            train_acc_count[ep] = tr_accuracy
            train_count_map_loss[ep], train_full_map_loss[ep] = ep_tr_map_loss
            train_acc_map[ep] = map_acc
            train_loss[ep] = ep_tr_loss  # optimized loss
            train_sh_loss[ep] = ep_tr_sh_loss

            ##### TEST ######
            confs = [None for _ in self.test_loaders]
            # shape_lum = product(config.test_shapes, config.lum_sets)
            for ts, test_loader in enumerate(self.test_loaders):
                epoch_te_loss, epoch_te_num_loss, te_accuracy, epoch_te_sh_loss, epoch_te_map_loss, epoch_df, conf, te_map_acc = self.test(test_loader, ep)
                epoch_df['train shapes'] = str(config.train_shapes)
                epoch_df['test shapes'] = str(test_loader.shapes)  # str(test_shapes)
                epoch_df['test lums'] = str(test_loader.lums)  # str(lums)
                epoch_df['testset'] = test_loader.testset
                epoch_df['viewing'] = test_loader.viewing
                epoch_df['repetition'] = config.rep
                test_results = pd.concat((test_results, epoch_df), ignore_index=True) # detailed 
                
                test_count_num_loss[ts][ep] = epoch_te_num_loss
                test_acc_count[ts][ep] = te_accuracy
                test_acc_map[ts][ep] = te_map_acc
                test_count_map_loss[ts][ep], test_full_map_loss[ts][ep] = epoch_te_map_loss
                test_loss[ts][ep] = epoch_te_loss
                test_sh_loss[ts][ep] = epoch_te_sh_loss
                test_losses = (test_loss, test_count_num_loss, test_count_map_loss, test_sh_loss)
                test_accs = (test_acc_count, test_acc_map)
                confs[ts] = conf

            if not ep % 10 or ep == n_epochs - 1 or ep==1:
                train_num_losses = (train_count_num_loss, train_dist_num_loss, train_all_num_loss)
                train_map_losses = (train_count_map_loss, train_dist_map_loss, train_full_map_loss)
                train_accs = (train_acc_count, train_acc_dist, train_acc_all, train_acc_map)
                train_losses = (train_num_losses, train_map_losses, train_sh_loss)
                # self.plot_performance(test_results, train_losses, train_accs, confs, ep + 1, config)
                self.plot_performance_quick(test_losses, test_accs, train_losses, train_accs, confs, ep + 1, config)
            epoch_timer.stop_timer()
            if isinstance(test_loss, list):
                print(f'Epoch {ep}. LR={self.optimizer.param_groups[0]["lr"]:.4}')
                # print(f'Train (Count/Dist/All) Num Loss={train_count_num_loss[ep]:.4}/{train_dist_num_loss[ep]:.4}/{train_all_num_loss[ep]:.4} \t Accuracy={train_acc_count[ep]:.3}%/{train_acc_dist[ep]:.3}%/{train_acc_all[ep]:.3}')
                # Shape loss: {train_sh_loss[ep]:.4}')
                # print(f'Train (Count/Dist/All) Map Loss={train_count_map_loss[ep]:.4}/{train_dist_map_loss[ep]:.4}/{train_full_map_loss[ep]:.4}')
                # print(f'Test (Count/Dist/All) Num Loss={test_count_num_loss[-2][ep]:.4}/{test_dist_num_loss[-2][ep]:.4}/{test_all_num_loss[-2][ep]:.4} \t Accuracy={test_acc_count[-2][ep]:.3}%/{test_acc_dist[-2][ep]:.3}%/{test_acc_all[-2][ep]:.3}')
                # print(f'Test (Count/Dist/All) Map Loss={test_count_map_loss[-2][ep]:.4}/{test_dist_map_loss[-2][ep]:.4}/{test_full_map_loss[-2][ep]:.4}')
                # -2 to get ood_free
                print(f'Train Loss={train_loss[ep]:.4} \t Accuracy={train_acc_count[ep]:.3}% \t Map Accuracy={train_acc_map[ep]:.3}%' )
                print(f'Test Val (Free/Fixed) Loss={test_count_num_loss[0][ep]:.4}/{test_count_num_loss[1][ep]:.4} \t Accuracy={test_acc_count[0][ep]:.3}%/{test_acc_count[1][ep]:.3}%')
                print(f'Test OOD (Free/Fixed) Loss={test_count_num_loss[2][ep]:.4}/{test_count_num_loss[3][ep]:.4} \t Accuracy={test_acc_count[2][ep]:.3}%/{test_acc_count[3][ep]:.3}%')
                print(f'Test Val (Free/Fixed) Map Loss={test_count_map_loss[0][ep]:.4}/{test_count_map_loss[1][ep]:.4} ')
                print(f'Test OOD (Free/Fixed) Map Loss={test_count_map_loss[2][ep]:.4}/{test_count_map_loss[3][ep]:.4} ')
                if config.learn_shape:
                    print(f'Test Val (Free/Fixed) Shape Loss={test_sh_loss[0][ep]:.4}/{test_sh_loss[1][ep]:.4} ')
                    print(f'Test OOD (Free/Fixed) Shape Loss={test_sh_loss[2][ep]:.4}/{test_sh_loss[3][ep]:.4} ')

            # else:
            #     print(f'Epoch {ep}. LR={optimizer.param_groups[0]["lr"]:.4} \t (Train/Test) Num Loss={train_num_loss[ep]:.4}/{test_num_loss[ep]:.4}/ \t Accuracy={train_acc[ep]:.3}%/{test_acc[ep]:.3}% \t Shape loss: {train_sh_loss[ep]:.5} \t Map loss: {train_map_loss[ep]:.5}')
        
        # Save network activations
        if config.save_act:
            print('Saving activations...')
            self.save_activations(self.model, self.test_loaders, base_name + '_trained', config)

        train_num_losses = (train_count_num_loss, train_dist_num_loss, train_all_num_loss)
        train_map_losses = (train_count_map_loss, train_dist_map_loss, train_full_map_loss)
        train_losses = (train_num_losses, train_map_losses, train_sh_loss)
        train_accs = (train_acc_count, train_acc_dist, train_acc_all)
        test_num_losses = (test_count_num_loss, test_dist_num_loss, test_all_num_loss)
        test_map_losses = (test_count_map_loss, test_dist_map_loss, test_full_map_loss)
        test_losses = (test_num_losses, test_map_losses, test_sh_loss)
        test_accs = (test_acc_count, test_acc_map, test_acc_dist, test_acc_all)

        # res_tr  = [train_loss, train_acc, train_num_loss, train_sh_loss, train_full_map_loss, train_count_map_loss]
        # res_te = [test_loss, test_acc, test_num_loss, test_sh_loss, test_full_map_loss, test_count_map_loss, confs, test_results]
        res_tr = [train_losses, train_accs]
        res_te = [test_losses, test_accs,  confs, test_results]
        results_list = res_tr + res_te
        return self.model, results_list

    @torch.no_grad()
    def test(self, loader, ep):
        noreduce = True
        self.model.eval()
        config = self.config
        device = config.device
        n_correct = 0
        # correct_map  = 0
        f1_sum = 0
        epoch_loss = 0
        num_epoch_loss = 0
        count_map_epoch_loss = 0
        # count_map_epoch_loss = 0
        shape_epoch_loss = 0
        # if 'unique' in config.challenge:
        #     max_num = 3
        #     min_num = 1
        #     confusion_matrix = np.zeros((max_num, max_num))
        # else:
        #     confusion_matrix = np.zeros((self.nclasses-config.min_num, self.nclasses-config.min_num))
        confusion_matrix = None
        test_results = pd.DataFrame()
        # for i, (input, target, locations, shape_label, pass_count) in enumerate(loader):
        for i, (_, input, target, num_dist, all_loc, shape_label, pass_count) in enumerate(loader):
            input = input.to(device)
            input_dim = input.shape[0]
            n_glimpses = input.shape[1]
            batch_results = pd.DataFrame()
            hidden = self.model.initHidden(input_dim)
            hidden = hidden.to(device)

            for t in range(n_glimpses):
                pred_num, pred_shape, map, hidden, _, _ = self.model(input[:, t, :], hidden)
                shape_loss = 0
                if config.learn_shape:
                    shape_loss_mse = criterion_mse(pred_shape, shape_label[:, t, :])#*10
                    # shape_loss_ce = criterion(pred_shape, shape_label[:, t, :])
                    shape_loss += shape_loss_mse #+ shape_loss_ce

            losses, pred = self.get_losses(pred_num, target, map, all_loc, ep, noreduce)
            loss, num_loss, map_loss, map_loss_to_add = losses
            if config.learn_shape:
                shape_loss /= n_glimpses
                loss += shape_loss
                shape_epoch_loss += shape_loss.item()
            else:
                shape_epoch_loss += -1
            correct = pred.eq(target.view_as(pred))
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                # Somehow before it was like just the first column was going
                # into batch_results, so just map loss for the first out of
                # nine location, instead of the average over all locations.
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                batch_results['full map loss'] = map_loss.detach().cpu().numpy()
                # batch_results['count map loss'] = count_map_loss.detach().cpu().numpy()
            except:
                # batch_results['map loss'] = np.ones(loss.shape) * -1
                batch_results['full map loss'] = np.ones(loss.shape) * -1
                batch_results['count map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            batch_results['shape loss'] = shape_loss.detach().cpu().numpy() if self.config.learn_shape else -1
            batch_results['epoch'] = ep
            test_results = pd.concat((test_results, batch_results))

            n_correct += pred.eq(target.view_as(pred)).sum().item()
            
            map_pred = torch.round(torch.sigmoid(map))
            correct_map = map_pred.eq(all_loc)*1.0
            positive = map_pred.eq(1.)*1.0
            true_positive = torch.logical_and(correct_map, positive) * 1.0
            precision = true_positive.sum(dim=0) / positive.sum(dim=0)
            true = all_loc.sum(dim=0)
            recall = true_positive.sum(dim=0)/true
            f1 = 2*((precision * recall)/(precision + recall)) 
            f1_sum += f1.nanmean().item()
            
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            # if not isinstance(map_loss_to_add, int):
            #     map_loss_to_add = map_loss_to_add.item()
            count_map_epoch_loss += map_loss_to_add
            # class-specific analysis and confusion matrix
            # c = (pred.squeeze() == target)
            if self.config.use_loss != 'map':
                confusion_matrix = self.update_confusion(target, pred, num_dist, confusion_matrix)

        # These two lines should be the same
        # map_epoch_loss / len(loader.dataset)
        # test_results['map loss'].mean()
        accuracy = 100. * (n_correct/len(loader.dataset))
        # map_acc = 100 * (correct_map/(len(loader)))
        map_f1 = 100* (f1_sum/(len(loader)))

        
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        # map_epoch_loss /= len(loader)
        if config.use_loss == 'num':
            # map_epoch_loss /= len(loader)
            count_map_epoch_loss /= len(loader)
        else:
            count_map_epoch_loss /= len(loader.dataset)
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss /= len(loader) #* n_glimpses
        return (epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss,
                map_epoch_loss, test_results, confusion_matrix, map_f1)
    
    def train(self, loader, ep):
        self.model.train()
        noreduce = False
        config = self.config
        correct = 0
        f1_sum = 0
        epoch_loss = 0
        num_epoch_loss = 0
        # map_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0

        for i, (_, input, target, num_dist, locations, shape_label, _) in enumerate(loader):
            # assert all(locations.sum(dim=1) == target)
            input = input.to(config.device)
            n_glimpses = input.shape[1]
            seq_len = input.shape[1]
            if self.shuffle:
                # Shuffle glimpse order on each batch
                # for i, row in enumerate(input):
                    # input[i, :, :] = row[torch.randperm(seq_len), :]
                perm = torch.randperm(seq_len)
                input = input[:, perm, :]
                shape_label = shape_label[:, perm, :] # need to  shuffle the shape label to match 
                
            input_dim = input.shape[0]

            self.model.zero_grad()
            hidden = self.model.initHidden(input_dim)
            hidden = hidden.to(config.device)

            for t in range(n_glimpses):
                pred_num, pred_shape, map, hidden, _, _ = self.model(input[:, t, :], hidden)
                shape_loss=0
                if config.learn_shape:
                    shape_loss_mse = criterion_mse(pred_shape, shape_label[:, t, :])
                    # shape_loss_ce = criterion(pred_shape, shape_label[:, t, :])
                    shape_loss += shape_loss_mse #+ shape_loss_ce
                    
                    # shape_loss.backward(retain_graph=True)

            losses, pred = self.get_losses(pred_num, target, map, locations, ep, noreduce)
            loss, num_loss, map_loss, map_loss_to_add = losses
           
            if config.learn_shape:
                shape_loss /= n_glimpses
                loss += shape_loss
                shape_epoch_loss += shape_loss.item()
            else:
                shape_epoch_loss += -1

            loss.backward()
            # Debugging code to monitor gradient norm at each layer
            # if i == len(loader) - 1:
            #     for name, layer in self.model.named_parameters():
            #         if layer.grad is not None:
            #             print(f'{name}:\t\t{layer.grad.norm().item():.4f}')
            #         else:
            #             print(f'{name}:\t\tgrad is None')
            nn.utils.clip_grad_norm_(self.model.parameters(), 2)
            self.optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            map_pred = torch.round(torch.sigmoid(map))
            correct_map = map_pred.eq(locations)*1.0
            positive = map_pred.eq(1.)*1.0
            true_positive = torch.logical_and(correct_map, positive) * 1.0
            precision = true_positive.sum(dim=0) / positive.sum(dim=0)
            true = locations.sum(dim=0)
            recall = true_positive.sum(dim=0)/true
            f1 = 2*((precision * recall)/(precision + recall)) 
            f1_sum += f1.nanmean().item()
            
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            # if not isinstance(map_loss_to_add, int):
                # map_loss_to_add = map_loss_to_add.item()
            count_map_epoch_loss += map_loss_to_add
            # count_map_epoch_loss += count_map_loss_to_add 
            
        accuracy = 100. * (correct/len(loader.dataset))
        # map_acc = 100 * (correct_map/(len(loader)))
        map_f1 = 100. * (f1_sum/len(loader))
        self.current_map_f1 = map_f1
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        if self.scheduler is not None:
            self.scheduler.step()
        # self.scheduler.step(epoch_loss)
        # self.scheduler.step(accuracy)
        # full_map_epoch_loss /= len(loader)
        count_map_epoch_loss /= len(loader)
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss /= len(loader) #* n_glimpses
        return epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss, map_epoch_loss, map_f1

    def plot_performance(self, test_results, train_losses, train_acc, confs, ep, config):

        base_name = config.base_name
        test_results['accuracy'] = test_results['correct'].astype(int)*100
        # data = test_results[test_results['test shapes'] == str(test_shapes) and test_results['test lums'] == str(lums)]
        data = test_results
        # max_pass = max(data['pass count'].max(), 6)
        # data = data[data['pass count'] < max_pass]
        self.make_loss_plot(data, train_losses, ep, config)
        # sns.countplot(data=test_results[test_results['correct']==True], x='epoch', hue='pass count')
        # plt.savefig(f'figures/toy/test_correct_{base_name}.png', dpi=300)
        # plt.close()

        (train_acc_count, _, _) = train_acc
        # accuracy = data.groupby(['epoch', 'pass count']).mean()
        # accuracy = data.groupby(['epoch', 'test shapes', 'test lums', 'pass count']).mean(numeric_only=True)
        accuracy = data.groupby(['epoch', 'testset', 'viewing']).mean(numeric_only=True)

        plt.plot(train_acc_count[:ep], ':', color='green', label='training accuracy')
        # sns.lineplot(data=accuracy, x='epoch', hue='test shapes',
        #             style='test lums', y='accuracy', alpha=0.7)
        sns.lineplot(data=accuracy, x='epoch', hue='testset',
                    style='viewing', y='accuracy', alpha=0.7)
        plt.legend()
        plt.grid()
        title = f'{config.model_type} trainon-{config.train_on} train_shapes-{config.train_shapes}'
        plt.title(title)
        plt.ylim([0, 102])
        plt.ylabel('Accuracy on number task')
        plt.savefig(f'{fig_dir}/accuracy_{base_name}.png', dpi=300)
        plt.close()

        # by integration score plot
        # accuracy = accuracy[]
        # plt.plot(train_acc_count[:ep + 1], ':', color='green', label='training accuracy')
        # sns.lineplot(data=accuracy, x='epoch', hue='pass count',
        #              y='accuracy', alpha=0.7)
        # plt.legend()
        # plt.grid()
        # plt.title(title)
        # plt.ylim([0, 102])
        # plt.ylabel('Accuracy on number task')
        # plt.savefig(f'figures/toy/letters/accuracy_byintegration_{base_name}.png', dpi=300)
        # plt.close()

        # acc_on_difficult = accuracy.loc[ep, 5.0]['accuracy']
        # print(f'Testset {ts}, Accuracy on level 5 difficulty: {acc_on_difficult}')
        if self.config.use_loss != 'map':
            self.plot_confusion(confs)
            
    def plot_performance_quick(self, test_losses, test_accs, train_losses, train_acc, confs, ep, config):
        plt.style.use('tableau-colorblind10')
        base_name = config.base_name
        # test_results['accuracy'] = test_results['correct'].astype(int)*100
        # # data = test_results[test_results['test shapes'] == str(test_shapes) and test_results['test lums'] == str(lums)]
        # data = test_results
        # max_pass = max(data['pass count'].max(), 6)
        # data = data[data['pass count'] < max_pass]
        self.make_loss_plot_quick(test_losses, train_losses, ep, config)
        # sns.countplot(data=test_results[test_results['correct']==True], x='epoch', hue='pass count')
        # plt.savefig(f'figures/toy/test_correct_{base_name}.png', dpi=300)
        # plt.close()
        (train_acc_count, _, _, train_map_acc) = train_acc
        (te_acc_count, te_acc_map) = test_accs
        # accuracy = data.groupby(['epoch', 'pass count']).mean()
        # accuracy = data.groupby(['epoch', 'test shapes', 'test lums', 'pass count']).mean(numeric_only=True)
        # accuracy = data.groupby(['epoch', 'testset', 'viewing']).mean(numeric_only=True)
        fig, (ax1, ax2) = plt.subplots(1,2)
        ax1.plot(train_acc_count[:ep], ':', color='green', label='training accuracy')
        # sns.lineplot(data=accuracy, x='epoch', hue='test shapes',
        #             style='test lums', y='accuracy', alpha=0.7)
        # sns.lineplot(data=accuracy, x='epoch', hue='testset',
        #             style='viewing', y='accuracy', alpha=0.7)
        ax1.plot(te_acc_count[0][:ep], label='Validation')
        ax1.plot(te_acc_count[1][:ep], label='OODshape')
        ax1.plot(te_acc_count[2][:ep], label='OODlum')
        ax1.plot(te_acc_count[3][:ep], label='OODboth')
        ax1.set_title('Number Accuracy')
        ax1.legend()
        ax1.grid()
        # title = f'{config.model_type} trainon-{config.train_on} train_shapes-{config.train_shapes}'
        # plt.title(title)
        ax1.set_ylim([0, 102])
        # ax1.set_ylabel('Accuracy on number task')
        
        ax2.plot(train_map_acc[1:ep], ':', color='green', label='training accuracy')
        ax2.plot(te_acc_map[0][:ep], label='Validation')
        ax2.plot(te_acc_map[1][:ep], label='OODshape')
        ax2.plot(te_acc_map[2][:ep], label='OODlum')
        ax2.plot(te_acc_map[3][:ep], label='OODboth')
        ax2.set_ylim([0, 102])
        ax2.grid()
        ax2.set_title('Map F1')
        # ax2.set_ylabel('Accuracy on map task')
        plt.tight_layout()
        plt.savefig(f'{fig_dir}/accuracy_{base_name}.png', dpi=300)
        plt.close()

        # by integration score plot
        # accuracy = accuracy[]
        # plt.plot(train_acc_count[:ep + 1], ':', color='green', label='training accuracy')
        # sns.lineplot(data=accuracy, x='epoch', hue='pass count',
        #              y='accuracy', alpha=0.7)
        # plt.legend()
        # plt.grid()
        # plt.title(title)
        # plt.ylim([0, 102])
        # plt.ylabel('Accuracy on number task')
        # plt.savefig(f'figures/toy/letters/accuracy_byintegration_{base_name}.png', dpi=300)
        # plt.close()

        # acc_on_difficult = accuracy.loc[ep, 5.0]['accuracy']
        # print(f'Testset {ts}, Accuracy on level 5 difficulty: {acc_on_difficult}')
        if self.config.use_loss != 'map':
            self.plot_confusion(confs)

    def update_confusion(self, target, pred, num_dist, confusion_matrix):
        if confusion_matrix is None:
            if 'unique' in self.config.challenge:
                max_num = 3
                min_num = 1
                confusion_matrix = np.zeros((max_num, max_num))
            else:
                # confusion_matrix = np.zeros((self.nclasses-self.config.min_num, self.nclasses-self.config.min_num))
                confusion_matrix = np.zeros((self.nclasses, self.nclasses))
            
        for label, prediction in zip(target, pred):
            # confusion_matrix[label - self.to_subtract, prediction - self.to_subtract] += 1
            confusion_matrix[label, prediction] += 1
        return confusion_matrix

    def plot_confusion(self, confs):
        fig, axs = plt.subplots(2, 2, figsize=(19, 16))
        maxes = [mat.max() for mat in confs]
        vmax = max(maxes)
        axs = axs.flatten()
        shape_lum = product(self.config.test_shapes, self.config.lum_sets)
            # for i, (shape, lum) in enumerate(shape_lum):
        for i, (ax, (shape, lum)) in enumerate(zip(axs, shape_lum)):
            ax.matshow(confs[i], cmap='Greys', vmin=0, vmax=vmax)
            ax.set_aspect('equal', adjustable='box')
            ax.set_title(f'shapes={shape} lums={lum}')
            ax.set_xticks(self.ticks, self.ticklabels)
            ax.set_xlabel('Predicted Class')
            ax.set_ylabel('True Class')
            ax.set_yticks(self.ticks, self.ticklabels)
            # ax2 = ax.twinx()
            # ax2.set_yticks(ticks, np.sum(confs[i], axis=1))
        fig.tight_layout()
        plt.savefig(f'{fig_dir}/confusion_{self.config.base_name}.png', dpi=300)
        plt.close()

    def make_loss_plot(self, data, train_losses, ep, config):
        # train_num_loss, train_full_map_loss, _, train_sh_loss = train_losses
        (train_num_losses, train_map_losses, train_sh_loss) = train_losses
        (train_num_loss, _, _) = train_num_losses
        (train_count_map_loss, _, _) = train_map_losses
        ## PLOT LOSS FOR BOTH OBJECTIVES
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=[9,9], sharex=True)
        # sns.lineplot(data=data, x='epoch', y='num loss', hue='pass count', ax=ax1)
        ax1.plot(train_num_loss[:ep], ':', color='green', label='training loss')
        # sns.lineplot(data=data, x='epoch', y='num loss', hue='test shapes',
        #             style='test lums', ax=ax1, legend=False, alpha=0.7)
        sns.lineplot(data=data, x='epoch', y='num loss', hue='testset',
                    style='viewing', ax=ax1, legend=False, alpha=0.7)

        # ax1.legend(title='Integration Difficulty')
        ax1.set_ylabel('Number Loss')
        mt = config.model_type #+ '-nosymbol' if nonsymbolic else config.model_type
        # title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes} \n test_shapes-{test_shapes} useloss-{config.use_loss} lums-{lums}'
        title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes}'
        ax1.set_title(title)
        ylim = ax1.get_ylim()
        ax1.set_ylim([-0.05, ylim[1]])
        ax1.grid()
        # plt.savefig(f'figures/toy/test_num-loss_{base_name_test}.png', dpi=300)
        # plt.close()

        # sns.lineplot(data=data, x='epoch', y='map loss', hue='pass count', ax=ax2, estimator='mean')
        ax2.plot(train_count_map_loss[:ep], ':', color='green', label='training loss')
        # sns.lineplot(data=data, x='epoch', y='full map loss', hue='test shapes',
        #             style='test lums', ax=ax2, estimator='mean', legend=False, alpha=0.7)
        sns.lineplot(data=data, x='epoch', y='full map loss', hue='testset',
                    style='viewing', ax=ax2, estimator='mean', legend=False, alpha=0.7)
        ax2.set_ylabel('Count Map Loss')
        # plt.title(title)
        ylim = ax2.get_ylim()
        ax2.set_ylim([-0.05, ylim[1]])
        ax2.grid()

        ax3.plot(train_sh_loss[:ep], ':', color='green', label='training loss')
        if 'shape loss' in data.columns:
            # sns.lineplot(data=data, x='epoch', y='shape loss', hue='test shapes',
            #             style='test lums', ax=ax3, estimator='mean', alpha=0.7)
            sns.lineplot(data=data, x='epoch', y='shape loss', hue='testset',
                        style='viewing', ax=ax3, estimator='mean', alpha=0.7)
        ax3.set_ylabel('Shape Loss')
        # plt.title(title)
        ylim = ax3.get_ylim()
        ax3.set_ylim([-0.05, ylim[1]])
        ax3.grid()
        fig.tight_layout()
        # ax2.legend(title='Integration Difficulty')
        ax3.legend()
        plt.savefig(f'{fig_dir}/loss_{config.base_name}.png', dpi=300)
        plt.close()
        
    def make_loss_plot_quick(self, test_losses, train_losses, ep, config):
        plt.style.use('tableau-colorblind10')
        (test_loss, test_count_num_loss, test_count_map_loss, test_sh_loss) = test_losses
        (train_num_losses, train_map_losses, train_sh_loss) = train_losses
        (train_num_loss, _, _) = train_num_losses
        (train_count_map_loss, _, _) = train_map_losses
                ## PLOT LOSS FOR BOTH OBJECTIVES
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=[9,9], sharex=True)
        # sns.lineplot(data=data, x='epoch', y='num loss', hue='pass count', ax=ax1)
        ax1.plot(test_count_num_loss[0][:ep], label='validation loss', alpha=0.7)
        ax1.plot(test_count_num_loss[1][:ep], label='ood_shape loss', alpha=0.7)
        ax1.plot(test_count_num_loss[2][:ep], label='ood_lum loss', alpha=0.7)
        ax1.plot(test_count_num_loss[3][:ep], label='ood_both loss', alpha=0.7)
        ax1.plot(train_num_loss[:ep], ':', color='green', label='training loss')
        ax1.set_ylabel('Number Loss')
        mt = config.model_type #+ '-nosymbol' if nonsymbolic else config.model_type
        # title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes} \n test_shapes-{test_shapes} useloss-{config.use_loss} lums-{lums}'
        title = f'{mt} trainon-{config.train_on} train_shapes-{config.train_shapes}'
        ax1.set_title(title)
        ylim = ax1.get_ylim()
        ax1.set_ylim([-0.05, ylim[1]])
        ax1.grid()
        ax1.legend()
        # plt.savefig(f'figures/toy/test_num-loss_{base_name_test}.png', dpi=300)
        # plt.close()

        # sns.lineplot(data=data, x='epoch', y='map loss', hue='pass count', ax=ax2, estimator='mean')
        ax2.plot(test_count_map_loss[0][:ep], label='validation loss', alpha=0.7)
        ax2.plot(test_count_map_loss[1][:ep], label='ood_shape loss', alpha=0.7)
        ax2.plot(test_count_map_loss[2][:ep], label='ood_lum loss', alpha=0.7)
        ax2.plot(test_count_map_loss[3][:ep], label='ood_both loss', alpha=0.7)
        ax2.plot(train_count_map_loss[:ep], ':', color='green', label='training loss')
        ax2.legend()
        ax2.set_ylabel('Count Map Loss')
        # plt.title(title)
        ylim = ax2.get_ylim()
        ax2.set_ylim([-0.05, ylim[1]])
        ax2.grid()
        
        ax3.plot(test_sh_loss[0][:ep], label='validation loss', alpha=0.7)
        ax3.plot(test_sh_loss[1][:ep], label='ood_shape loss', alpha=0.7)
        ax3.plot(test_sh_loss[2][:ep], label='ood_lum loss', alpha=0.7)
        ax3.plot(test_sh_loss[3][:ep], label='ood_both loss', alpha=0.7)
        ax3.plot(train_sh_loss[:ep], ':', color='green', label='training loss')
        ax3.legend()
        ax3.set_ylabel('Shape Loss')
        # plt.title(title)
        ylim = ax3.get_ylim()
        ax3.set_ylim([-0.05, ylim[1]])
        ax3.grid()
        
        
        plt.savefig(f'{fig_dir}/loss_{config.base_name}.png', dpi=300)
        plt.close()
        
        

    @torch.no_grad()
    def save_activations(self, model, test_loaders, basename, config):
        """
        Pass data through model and save activations at various points in the architecture.

        Include other trial details: number of targets, number of distractors, model prediction
        (softmax outputs), xy coordinates (in pixels).

        Args:
            model (torch nn.Module): The torch model whose activations should be calculated and saved
            test_loaders (list): torch DataLoaders for each of the test datasets
            basename (str): base file name indicating relevant model, data, and training parameters
            config (Namespace): configuration variables for these model run
        """
        model.eval()
        device = self.config.device
        softmax = nn.Softmax(dim=1)
        shape_lum = product(config.test_shapes, config.lum_sets)
        n_glimpses = config.n_glimpses
        test_names = ['validation', 'new-luminances', 'new-shapes', 'new_both']
        # test_names = ['val_free', 'val_fixed', 'ood_free', 'ood_fixed']
        is_cnn = 'cnn' in config.model_type and 'ventral' not in config.model_type
        sets_to_save = [1, 3] if is_cnn else [3] # also save acts of validation set if CNN
        # 
        for ts, (test_loader, (test_shapes, lums)) in enumerate(zip(test_loaders, shape_lum)):
        # only save new-both test set for now
        # ts = 3
        # test_loader = test_loaders[-1]
            if ts not in sets_to_save:
                continue
            # Initialize
            start = 0
            test_size = len(test_loader.dataset)
            
            if is_cnn:
                premap_act = np.zeros((test_size, model.fc1_size))
            else:
                hidden_act = np.zeros((test_size, n_glimpses, config.h_size))
                # premap_act = np.zeros((test_size, n_glimpses, config.h_size))
                # penult_act = np.zeros((test_size, n_glimpses, config.grid**2))
            glimpse_coords = np.zeros((test_size, n_glimpses, 2))
            numerosity = np.zeros((test_size,))
            dist_num = np.zeros((test_size,))
            predicted_num = np.zeros((test_size, model.output_size))
            correct = np.zeros((test_size,))
            index = np.zeros((test_size,))
            # Loop through minibatches
            for i, (ind, input_, target, num_dist, all_loc, shape_label, pass_count) in enumerate(test_loader):
                input_ = input_.to(device)
                batch_size = input_.shape[0]
                index[start: start + batch_size] = ind.numpy()
                numerosity[start: start + batch_size] = target.cpu().detach().numpy()
                dist_num[start: start + batch_size] = num_dist.cpu().detach().numpy()
                if is_cnn:
                    pred_num, _, premap  = model(input_)
                    premap_act[start: start + batch_size] = premap.cpu().detach().numpy()
                else:
                    hidden = model.initHidden(batch_size).to(device)
                    xy = input_[:, :, :2].cpu().detach().numpy()
                    glimpse_coords[start: start + batch_size] = xy
                    for t in range(n_glimpses):
                        pred_num, _, _, hidden, premap, penult = model(input_[:, t, :], hidden)
                        hidden_act[start: start + batch_size, t] = hidden.cpu().detach().numpy()
                        # premap_act[start: start + batch_size, t] = premap.cpu().detach().numpy()
                        # penult_act[start: start + batch_size, t] = penult.cpu().detach().numpy()

                pred = pred_num.argmax(dim=1, keepdim=True)
                predicted_num[start: start + batch_size] = softmax(pred_num).cpu().detach().numpy()
                correct[start: start + batch_size] = pred.eq(target.view_as(pred)).cpu().detach().numpy().squeeze()
                
                start += batch_size
            # Retrieve image metadata in order for this epoch
            image_data = self.image_metadata[ts]
            # Can't save None to .mat so replace with empty array
            target_locations = np.array([np.array([]) if tl is None else tl for tl in image_data.target_coords_scaled[index].values], dtype=object)
            distractor_locations = np.array([np.array([]) if dl is None else dl for dl in image_data.distract_coords_scaled[index].values], dtype=object)
            # Save to file
            savename = f'activations/{basename}_test-{test_names[ts]}'
            if is_cnn:
                # Compressed numpy
                np.savez(savename, numerosity=numerosity, num_distractor=dist_num, 
                        act_premap=premap_act, 
                        predicted_num=predicted_num, correct=correct, 
                        target_locations=target_locations,
                        distractor_locations=distractor_locations)
                to_save = {'numerosity':numerosity, 'num_distractor':dist_num, 
                        'act_premap':premap_act, 
                        'predicted_num':predicted_num, 'correct':correct, 
                        'target_locations': target_locations,
                        'distractor_locations': distractor_locations}
            else:
                # Put into pandas dataframe
                # image_data.loc(index)
                # variables = [index, numerosity, dist_num, hidden_act, predicted_num, correct, glimpse_coords]
                # columns=['index', 'numerosity', 'num_distractor', 'act_hidden', 'predicted_num', 'correct', 'glimpse_xy']
                # df = pd.DataFrame(columns=columns)
                # for var, col in zip(variables, columns): df[col] = var
                # df = df.join(image_data)
                
                # Compressed numpy
                np.savez(savename, numerosity=numerosity, num_distractor=dist_num, 
                        act_hidden=hidden_act, 
                        # act_premap=premap_act, act_penult=penult_act, 
                        predicted_num=predicted_num, correct=correct,
                        glimpse_xy=glimpse_coords, 
                        target_locations=target_locations,
                        distractor_locations=distractor_locations)
                to_save = {'numerosity':numerosity, 'num_distractor':dist_num, 
                            'act_hidden':hidden_act, 
                            # 'act_premap':premap_act, 'act_penult':penult_act, 
                            'predicted_num':predicted_num, 'correct':correct,
                            'glimpse_xy':glimpse_coords, 
                            'target_locations': target_locations,
                            'distractor_locations': distractor_locations}
            # MATLAB
            savemat(savename + '.mat', to_save)

    def get_map_loss(self, map, locations, noreduce=False):
        if noreduce:
            all_map_loss = self.criterion_bce_full_noreduce(map, locations)
            map_loss = all_map_loss.mean(axis=1)
            map_loss_to_add = map_loss.sum().item()
        else:
            map_loss = self.criterion_bce_full(map, locations)
            map_loss_to_add = map_loss.item()
        return map_loss, map_loss_to_add
    
    def get_losses(self, pred_num, target, map, locations, ep, noreduce):
        # Calculate number classification loss
        if noreduce:
            num_loss = criterion_noreduce(pred_num, target)
            pred = pred_num.argmax(dim=1, keepdim=True)
        else:
            num_loss = criterion(pred_num, target)
            pred = pred_num.argmax(dim=1, keepdim=True)
        
        # Calculate other losses and assign which to be optimized
        if self.config.use_loss == 'num':
            loss = num_loss
            map_loss_to_add = -1
            map_loss = None
        elif self.config.use_loss == 'map':
            map_loss, map_loss_to_add = self.get_map_loss(map, locations, noreduce)
            loss = map_loss
        elif self.config.use_loss == 'both':
            map_loss, map_loss_to_add = self.get_map_loss(map, locations, noreduce)
            loss = num_loss + map_loss
        elif self.config.use_loss == 'map_then_both':
            map_loss, map_loss_to_add = self.get_map_loss(map, locations, noreduce)
            if ep < 100:
            # if self.current_map_f1 < 99:
                loss = map_loss
            else:
                loss = num_loss + map_loss
        
        losses = (loss, num_loss, map_loss, map_loss_to_add)
        return losses, pred


class TrainerDistract(Trainer):
    def __init__(self, model, loaders, test_xarray, config):
        super().__init__(model, loaders, test_xarray, config)
    
    def update_confusion(self, target, pred, num_dist, confusion_matrix):
        if confusion_matrix is None:
            # confusion_matrix = np.zeros((3, self.nclasses-self.config.min_num, self.nclasses-self.config.min_num))
            confusion_matrix = np.zeros((3, self.nclasses, self.nclasses))
        
        # for dist in [0, 1, 2]:
        for i, dist in enumerate(torch.unique(num_dist)):
            ind = num_dist == dist
            target_subset = target[ind]
            pred_subset = pred[ind]
            for j in range(target_subset.shape[0]):
                label = target_subset[j]
                # confusion_matrix[i, label-self.config.min_num, pred_subset[j]-self.config.min_num] += 1
                confusion_matrix[i, label, pred_subset[j]] += 1
        return confusion_matrix

    def plot_confusion(self, confs):
        distractor_set = [1, 2, 3]
        fig, axs = plt.subplots(len(distractor_set), 4, figsize=(19, 16))
        
        maxes = [mat.max() for mat in confs]
        vmax = max(maxes)
        # axs = axs.flatten()
        
        for j, dist in enumerate(distractor_set):
            shape_lum = product(self.config.test_shapes, self.config.lum_sets)
            for i, (shape, lum) in enumerate(shape_lum):
        # for i, (ax, (shape, lum)) in enumerate(zip(axs, shape_lum)):
                axs[j, i].matshow(confs[i][j, :, :], cmap='Greys', vmin=0, vmax=vmax)
                axs[j, i].set_aspect('equal', adjustable='box')
                axs[j, i].set_title(f'dist={dist} shapes={shape} lums={lum}')
                axs[j, i].set_xticks(self.ticks, self.ticklabels)
                axs[j, i].set_xlabel('Predicted Class')
                axs[j, i].set_ylabel('True Class')
                axs[j, i].set_yticks(self.ticks, self.ticklabels)
            # ax2 = ax.twinx()
            # ax2.set_yticks(ticks, np.sum(confs[i], axis=1))
        fig.tight_layout()
        plt.savefig(f'{fig_dir}/confusion_{self.config.base_name}.png', dpi=300)
        plt.close()


class FeedForwardTrainer(Trainer):
    def __init__(self, model, loaders, test_xarray, config):
        super().__init__(model, loaders, test_xarray, config)
    
    @torch.no_grad()
    def test(self, loader, ep):
        self.model.eval()
        noreduce = True
        config = self.config
        n_correct = 0
        correct_map = 0
        epoch_loss = 0
        num_epoch_loss = 0
        count_map_epoch_loss = 0
        confusion_matrix = None
        test_results = pd.DataFrame()
        # for i, (input, target, locations, shape_label, pass_count) in enumerate(loader):
        for i, (_, input, target, num_dist, all_loc, pass_count) in enumerate(loader):
            input = input.to(config.device)
            batch_results = pd.DataFrame()
            pred_num, map, _ = self.model(input)

            losses, pred = self.get_losses(pred_num, target, map, all_loc, ep, noreduce)
            loss, num_loss, map_loss, map_loss_to_add = losses
            correct = pred.eq(target.view_as(pred))
            
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                # Somehow before it was like just the first column was going
                # into batch_results, so just map loss for the first out of
                # nine location, instead of the average over all locations.
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                batch_results['full map loss'] = map_loss.detach().cpu().numpy()
                # batch_results['count map loss'] = count_map_loss.detach().cpu().numpy()
            except:
                # batch_results['map loss'] = np.ones(loss.shape) * -1
                batch_results['full map loss'] = np.ones(loss.shape) * -1
                batch_results['count map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            # batch_results['shape loss'] = shape_loss.detach().cpu().numpy() if self.config.learn_shape else -1
            batch_results['epoch'] = ep
            test_results = pd.concat((test_results, batch_results))

            n_correct += correct.sum().item()
            correct_map += (torch.round(torch.sigmoid(map)).eq(all_loc)*1.0).mean().item()
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            count_map_epoch_loss += map_loss_to_add
            # class-specific analysis and confusion matrix
            if self.config.use_loss != 'map':
                confusion_matrix = self.update_confusion(target, pred, num_dist, confusion_matrix)

        # These two lines should be the same
        # map_epoch_loss / len(loader.dataset)
        # test_results['map loss'].mean()

        accuracy = 100. * (n_correct/len(loader.dataset))
        map_acc = 100 * (correct_map/(len(loader)))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        # map_epoch_loss /= len(loader)
        if config.use_loss == 'num':
            # map_epoch_loss /= len(loader)
            count_map_epoch_loss /= len(loader)
        else:
            count_map_epoch_loss /= len(loader.dataset)
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss = -1
        return (epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss,
                map_epoch_loss, test_results, confusion_matrix, map_acc)
    
    def train(self, loader, ep):
        self.model.train()
        noreduce = False
        correct = 0
        correct_map = 0
        epoch_loss = 0
        num_epoch_loss = 0
        # map_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0
        for i, (_, input, target, _, locations, _) in enumerate(loader):
            input = input.to(self.config.device)
            # assert all(locations.sum(dim=1) == target)
            self.model.zero_grad()
            pred_num, map, _ = self.model(input)
            losses, pred = self.get_losses(pred_num, target, map, locations, ep, noreduce)
            loss, num_loss, _, map_loss_to_add = losses
            loss.backward()
            self.optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            correct_map += (torch.round(torch.sigmoid(map)).eq(locations)*1.0).mean().item()
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            # if not isinstance(map_loss_to_add, int):
                # map_loss_to_add = map_loss_to_add.item()
            count_map_epoch_loss += map_loss_to_add
            # count_map_epoch_loss += count_map_loss_to_add
        
        accuracy = 100. * (correct/len(loader.dataset))
        map_acc = 100 * (correct_map/(len(loader)))
        epoch_loss /= i+1
        if self.scheduler is not None:
            self.scheduler.step() # for scheduler that is not metric dependent
        # self.scheduler.step(epoch_loss)
        # self.scheduler.step(accuracy)
        num_epoch_loss /= i+1
        # full_map_epoch_loss /= len(loader)
        count_map_epoch_loss /= i+1
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss = -1
        return epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss, map_epoch_loss, map_acc

    @torch.no_grad()
    def save_activations(self, model, test_loaders, basename, config):
        pass
        # TODO
    
class FeedForwardTrainerDistract(FeedForwardTrainer, TrainerDistract):
    def __init__(self, model, loaders, test_xarray, config):
        super().__init__(model, loaders, test_xarray, config)
    
    
class RecurrentTrainer(Trainer):
    def __init__(self, model, loaders, test_xarray, config):
        super().__init__(model, loaders, test_xarray, config)
    
    @torch.no_grad()
    def test(self, loader, ep):
        self.model.eval()
        noreduce = True
        config = self.config
        n_correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        count_map_epoch_loss = 0
        confusion_matrix = None
        test_results = pd.DataFrame()
        # for i, (input, target, locations, shape_label, pass_count) in enumerate(loader):
        for i, (_, input, target, num_dist, all_loc, pass_count) in enumerate(loader):
            input = input.to(config.device)
            input_dim = input.shape[0]
            n_glimpses = config.n_glimpses
            batch_results = pd.DataFrame()
            hidden = self.model.initHidden(input_dim)
            hidden = hidden.to(self.config.device)

            for t in range(n_glimpses):
                pred_num, _, map, hidden, _, _ = self.model(input, hidden)
            pred_num, map, _ = self.model(input)

            losses, pred = self.get_losses(pred_num, target, map, all_loc, ep, noreduce)
            loss, num_loss, map_loss, map_loss_to_add = losses
            correct = pred.eq(target.view_as(pred))
            
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                # Somehow before it was like just the first column was going
                # into batch_results, so just map loss for the first out of
                # nine location, instead of the average over all locations.
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                batch_results['full map loss'] = map_loss.detach().cpu().numpy()
                # batch_results['count map loss'] = count_map_loss.detach().cpu().numpy()
            except:
                # batch_results['map loss'] = np.ones(loss.shape) * -1
                batch_results['full map loss'] = np.ones(loss.shape) * -1
                batch_results['count map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            # batch_results['shape loss'] = shape_loss.detach().cpu().numpy() if self.config.learn_shape else -1
            batch_results['epoch'] = ep
            test_results = pd.concat((test_results, batch_results))

            n_correct += correct.sum().item()
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            count_map_epoch_loss += map_loss_to_add
            # class-specific analysis and confusion matrix
            if self.config.use_loss != 'map':
                confusion_matrix = self.update_confusion(target, pred, num_dist, confusion_matrix)

        # These two lines should be the same
        # map_epoch_loss / len(loader.dataset)
        # test_results['map loss'].mean()

        accuracy = 100. * (n_correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        # map_epoch_loss /= len(loader)
        if config.use_loss == 'num':
            # map_epoch_loss /= len(loader)
            count_map_epoch_loss /= len(loader)
        else:
            count_map_epoch_loss /= len(loader.dataset)
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss = -1
        return (epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss,
                map_epoch_loss, test_results, confusion_matrix)
    
    def train(self, loader, ep):
        self.model.train()
        noreduce = False
        correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        # map_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0
        for i, (_, input, target, _, locations, _) in enumerate(loader):
            input = input.to(self.config.device)
            # assert all(locations.sum(dim=1) == target)
            self.model.zero_grad()
            input_dim = input.shape[0]
            n_glimpses = self.config.n_glimpses
            hidden = self.model.initHidden(input_dim)
            hidden = hidden.to(self.config.device)

            for t in range(n_glimpses):
                pred_num, _, map, hidden, _, _ = self.model(input, hidden)
            losses, pred = self.get_losses(pred_num, target, map, locations, ep, noreduce)
            loss, num_loss, _, map_loss_to_add = losses
            loss.backward()
            self.optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            # if not isinstance(map_loss_to_add, int):
                # map_loss_to_add = map_loss_to_add.item()
            count_map_epoch_loss += map_loss_to_add
            # count_map_epoch_loss += count_map_loss_to_add
        
        accuracy = 100. * (correct/len(loader.dataset))
        epoch_loss /= i+1
        if self.scheduler is not None:
            self.scheduler.step()
        # self.scheduler.step(epoch_loss)
        # self.scheduler.step(accuracy)
        num_epoch_loss /= i+1
        # full_map_epoch_loss /= len(loader)
        count_map_epoch_loss /= i+1
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss = -1
        return epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss, map_epoch_loss


class RecurrentTrainerDistract(RecurrentTrainer, TrainerDistract):
    def __init__(self, model, loaders, test_xarray, config):
        super().__init__(model, loaders, test_xarray, config)

class TorchRNNTrainer(Trainer):
    @torch.no_grad()
    def test(self, loader, ep):
        noreduce = True  # because we want per image predictions not just mean
        self.model.eval()
        config = self.config
        device = config.device
        n_correct = 0
        epoch_loss = 0
        num_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0
        confusion_matrix = None
        test_results = pd.DataFrame()
        # for i, (input, target, locations, shape_label, pass_count) in enumerate(loader):
        for i, (_, xy, pix, target, num_dist, all_loc, shape_label, pass_count) in enumerate(loader):
            xy = xy.to(device)
            pix = pix.to(device)
            n_glimpses = xy.shape[1]
            batch_results = pd.DataFrame()
            # hidden = self.model.initHidden(input_dim)
            # hidden = hidden.to(device)
            hidden = None
            for t in range(n_glimpses):
                pred_num, pred_shape, map, hidden, _, _ = self.model(xy[:, t], pix[:, t, :], hidden)
                if config.learn_shape:
                    shape_loss_mse = criterion_mse(pred_shape, shape_label[:, t, :])#*10
                    shape_loss_ce = criterion(pred_shape, shape_label[:, t, :])
                    shape_loss = shape_loss_mse#+ shape_loss_ce
                    shape_epoch_loss += shape_loss.item()
                else:
                    shape_epoch_loss += -1
            losses, pred = self.get_losses(pred_num, target, map, all_loc, ep, noreduce)
            # pred_num, pred_shape, map, _, _, _ = self.model(input)
            # losses, pred = self.get_losses(pred_num[:,-1,:], target, map[:, -1, :], all_loc, ep, noreduce)
            loss, num_loss, map_loss, map_loss_to_add = losses
            correct = pred.eq(target.view_as(pred))
            batch_results['pass count'] = pass_count.detach().cpu().numpy()
            batch_results['correct'] = correct.cpu().numpy()
            batch_results['predicted'] = pred.detach().cpu().numpy()
            batch_results['true'] = target.detach().cpu().numpy()
            batch_results['loss'] = loss.detach().cpu().numpy()
            try:
                # Somehow before it was like just the first column was going
                # into batch_results, so just map loss for the first out of
                # nine location, instead of the average over all locations.
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                # batch_results['map loss'] = map_loss.mean(axis=1).detach().cpu().numpy()
                batch_results['full map loss'] = map_loss.detach().cpu().numpy()
                # batch_results['count map loss'] = count_map_loss.detach().cpu().numpy()
            except:
                # batch_results['map loss'] = np.ones(loss.shape) * -1
                batch_results['full map loss'] = np.ones(loss.shape) * -1
                batch_results['count map loss'] = np.ones(loss.shape) * -1
            batch_results['num loss'] = num_loss.detach().cpu().numpy()
            batch_results['shape loss'] = -1 #shape_loss.detach().cpu().numpy() if self.config.learn_shape else -1
            batch_results['epoch'] = ep
            test_results = pd.concat((test_results, batch_results))

            n_correct += pred.eq(target.view_as(pred)).sum().item()
            epoch_loss += loss.mean().item()
            num_epoch_loss += num_loss.mean().item()
            # if not isinstance(map_loss_to_add, int):
            #     map_loss_to_add = map_loss_to_add.item()
            count_map_epoch_loss += map_loss_to_add
            # class-specific analysis and confusion matrix
            # c = (pred.squeeze() == target)
            if self.config.use_loss != 'map':
                confusion_matrix = self.update_confusion(target, pred, num_dist, confusion_matrix)

        # These two lines should be the same
        # map_epoch_loss / len(loader.dataset)
        # test_results['map loss'].mean()

        accuracy = 100. * (n_correct/len(loader.dataset))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        # map_epoch_loss /= len(loader)
        if config.use_loss == 'num':
            # map_epoch_loss /= len(loader)
            count_map_epoch_loss /= len(loader)
        else:
            count_map_epoch_loss /= len(loader.dataset)
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss /= len(loader) * n_glimpses

        return (epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss,
                map_epoch_loss, test_results, confusion_matrix)
    
    def train(self, loader, ep):
        self.model.train()
        noreduce = False
        config = self.config
        correct = 0
        correct_map = 0
        epoch_loss = 0
        num_epoch_loss = 0
        # map_epoch_loss = 0
        count_map_epoch_loss = 0
        shape_epoch_loss = 0
        accuracy=0

        for i, (_, xy, pix, target, num_dist, locations, shape_label, _) in enumerate(loader):
            # assert all(locations.sum(dim=1) == target)
            # input = input.to(config.device)
            xy = xy.to(config.device)
            pix = pix.to(config.device)
            n_glimpses = xy.shape[1]
            if self.shuffle:
                new_order = torch.randperm(n_glimpses)
                # Shuffle glimpse order on each batch
                # for i, row in enumerate(input):
                    # input[i, :, :] = row[torch.randperm(seq_len), :]
                xy = xy[:, new_order, :]
                pix = pix[:, new_order, :]
                shape_label = shape_label[:, new_order, :]

            self.model.zero_grad()
            # hidden = self.model.initHidden(input_dim)
            # hidden = hidden.to(config.device)
            hidden = None
            for t in range(n_glimpses):
                pred_num, pred_shape, map, hidden, _, _ = self.model(xy[:, t], pix[:, t, :], hidden)
                if config.learn_shape:
                    shape_loss_mse = criterion_mse(pred_shape, shape_label[:, t, :])#*10
                    shape_loss_ce = criterion(pred_shape, shape_label[:, t, :])
                    shape_loss = shape_loss_mse #+ shape_loss_ce
                    shape_epoch_loss += shape_loss.item()
                    shape_loss.backward(retain_graph=True)
                else:
                    shape_epoch_loss += -1
            losses, pred = self.get_losses(pred_num, target, map, locations, ep, noreduce)
            
            loss, num_loss, map_loss, map_loss_to_add = losses

            loss.backward()
            nn.utils.clip_grad_norm_(self.model.parameters(), 2)
            self.optimizer.step()

            correct += pred.eq(target.view_as(pred)).sum().item()
            correct_map += (torch.round(torch.sigmoid(map)).eq(locations)*1.0).mean().item()
            
            
            epoch_loss += loss.item()
            num_epoch_loss += num_loss.item()
            # if not isinstance(map_loss_to_add, int):
                # map_loss_to_add = map_loss_to_add.item()
            count_map_epoch_loss += map_loss_to_add
            # count_map_epoch_loss += count_map_loss_to_add 
        accuracy = 100. * (correct/len(loader.dataset))
        map_acc = 100 * (correct_map/(len(loader)))
        epoch_loss /= len(loader)
        num_epoch_loss /= len(loader)
        if self.scheduler is not None:
            self.scheduler.step()
        # self.scheduler.step(epoch_loss)
        # self.scheduler.step(accuracy)
        # full_map_epoch_loss /= len(loader)
        count_map_epoch_loss /= len(loader)
        map_epoch_loss = (count_map_epoch_loss, -1)
        shape_epoch_loss /= len(loader) * n_glimpses
        return epoch_loss, num_epoch_loss, accuracy, shape_epoch_loss, map_epoch_loss, map_acc
