# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
##############################################################################
#
# Based on:
# pix2pixHD (https://github.com/NVIDIA/pix2pixHD)
### Copyright (C) 2017 NVIDIA Corporation. All rights reserved.
### Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
import numpy as np
import torch
import os
from torch.autograd import Variable
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks

class Pix2PixHDModel(BaseModel):
    def name(self):
        return 'Pix2PixHDModel'

    def init_loss_filter(self, use_gan_feat_loss, use_vgg_loss, use_style_loss, use_recon_loss):
        flags = (True, use_gan_feat_loss, use_vgg_loss, use_style_loss, use_recon_loss, True, True)
        def loss_filter(g_gan, g_gan_feat, g_vgg, g_style, g_recon, d_real, d_fake):
            return [l for (l,f) in zip((g_gan,g_gan_feat,g_vgg,g_style,g_recon,d_real,d_fake),flags) if f]
        return loss_filter

    def initialize(self, opt):
        BaseModel.initialize(self, opt)
        if opt.resize_or_crop != 'none' or not opt.isTrain: # when training at full res this causes OOM
            torch.backends.cudnn.benchmark = True
        self.isTrain = opt.isTrain
        self.use_features = opt.instance_feat or opt.label_feat
        self.gen_features = self.use_features and not self.opt.load_features
        input_nc = opt.label_nc if opt.label_nc != 0 else opt.input_nc

        ##### define networks
        # Generator network
        netG_input_nc = input_nc
        if not opt.no_instance:
            netG_input_nc += 1
        if self.use_features:
            netG_input_nc += opt.feat_num
        self.netG = networks.define_G(netG_input_nc, opt.output_nc, opt.ngf, opt.netG,
                                      opt.n_downsample_global, opt.n_blocks_global, opt.n_local_enhancers,
                                      opt.n_blocks_local, opt.norm, gpu_ids=self.gpu_ids)

        # Discriminator network
        if self.isTrain:
            use_sigmoid = opt.no_lsgan
            netD_input_nc = input_nc + opt.output_nc
            if not opt.no_instance:
                netD_input_nc += 1
            self.netD = networks.define_D(netD_input_nc, opt.ndf, opt.n_layers_D, opt.d_norm, use_sigmoid,
                                          opt.num_D, not opt.no_ganFeat_loss, gpu_ids=self.gpu_ids)

        ### Encoder network
        if self.gen_features:
            self.netE = networks.define_G(opt.output_nc, opt.feat_num, opt.nef, 'encoder',
                                          opt.n_downsample_E, norm=opt.norm, gpu_ids=self.gpu_ids)
        if self.opt.verbose:
                print('---------- Networks initialized -------------')

        # load networks
        if not self.isTrain or opt.continue_train or opt.load_pretrain:
            pretrained_path = '' if not self.isTrain else opt.load_pretrain
            self.load_network(self.netG, 'G', opt.which_epoch, pretrained_path)
            if self.isTrain:
                self.load_network(self.netD, 'D', opt.which_epoch, pretrained_path)
            if self.gen_features:
                self.load_network(self.netE, 'E', opt.which_epoch, pretrained_path)

        # set loss functions and optimizers
        if self.isTrain:
            if opt.pool_size > 0 and (len(self.gpu_ids)) > 1:
                raise NotImplementedError("Fake Pool Not Implemented for MultiGPU")
            self.fake_pool = ImagePool(opt.pool_size)
            self.old_lr = opt.lr

            # define loss functions
            self.loss_filter = self.init_loss_filter(not opt.no_ganFeat_loss, not opt.no_vgg_loss, not opt.no_style_loss, not opt.no_recon_loss)

            self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan, tensor=self.Tensor)
            self.criterionFeat = torch.nn.L1Loss()
            if not opt.no_style_loss:
                self.criterionVGG = networks.VGGLosses(self.gpu_ids)
            elif not opt.no_vgg_loss:
                self.criterionVGG = networks.VGGLoss(self.gpu_ids)

            if not opt.no_recon_loss:
                self.criterionRecon = torch.nn.L1Loss()

            # Names so we can breakout loss
            self.loss_names = self.loss_filter('G_GAN_Encode','G_GAN_Feat','G_VGG','G_STYLE_VGG','G_RECON','D_real', 'D_fake')


            # initialize optimizers
            # optimizer G
            if opt.niter_fix_global > 0:
                import sys
                if sys.version_info >= (3,0):
                    finetune_list = set()
                else:
                    from sets import Set
                    finetune_list = Set()

                params_dict = dict(self.netG.named_parameters())
                params = []
                for key, value in params_dict.items():
                    if key.startswith('model' + str(opt.n_local_enhancers)):
                        params += [value]
                        finetune_list.add(key.split('.')[0])
                print('------------- Only training the local enhancer network (for %d epochs) ------------' % opt.niter_fix_global)
                print('The layers that are finetuned are ', sorted(finetune_list))
            else:
                params = list(self.netG.parameters())
            if self.gen_features:
                params += list(self.netE.parameters())
            self.optimizer_G = torch.optim.Adam(params, lr=opt.lr, betas=(opt.beta1, 0.999))

            # optimizer D
            if self.opt.d_norm == 'spectral':
                print('Set spectral norm optimizer')
                # because the spectral normalization module creates parameters that don't require gradients (u and v), we don't want to
                # optimize these using sgd. We only let the optimizer operate on parameters that _do_ require gradients
                params = filter(lambda p: p.requires_grad, self.netD.parameters())
                self.optimizer_D = torch.optim.Adam(params, lr=opt.lr, betas=(opt.beta1, 0.999))
            else:
                params = list(self.netD.parameters())
                self.optimizer_D = torch.optim.Adam(params, lr=opt.lr, betas=(opt.beta1, 0.999))

    def get_z_random(self, nz, random_type='gauss'):
        if random_type == 'uni':
            z = torch.rand(nz, requires_grad=False) * 2.0 - 1.0
        elif random_type == 'gauss':
            z = torch.randn(nz, requires_grad=False)
        return z #.cuda()

    def encode_input(self, label_map, inst_map=None, real_image=None, feat_map=None, infer=False):
        if self.opt.label_nc == 0:
            input_label = label_map.data #.cuda()
        else:
            # create one-hot vector for label map
            size = label_map.size()
            oneHot_size = (size[0], self.opt.label_nc, size[2], size[3])
            input_label = torch.FloatTensor(torch.Size(oneHot_size)).zero_() #torch.cuda.FloatTensor(torch.Size(oneHot_size)).zero_()
            input_label = input_label.scatter_(1, label_map.data.long(), 1.0)
            if self.opt.data_type == 16:
                input_label = input_label.half()

        # get edges from instance map
        if not self.opt.no_instance:
            inst_map = inst_map.data #.cuda()
            edge_map = self.get_edges(inst_map)
            input_label = torch.cat((input_label, edge_map), dim=1)
        input_label = Variable(input_label, volatile=infer)

        # real images for training
        if real_image is not None:
            real_image = Variable(real_image.data) #.cuda())

        # instance map for feature encoding
        if self.use_features:
            # get precomputed feature maps
            if self.opt.load_features:
                feat_map = Variable(feat_map.data) #.cuda())

        return input_label, inst_map, real_image, feat_map

    def discriminate(self, input_label, test_image, use_pool=False):
        input_concat = torch.cat((input_label, test_image.detach()), dim=1)
        if use_pool:
            fake_query = self.fake_pool.query(input_concat)
            return self.netD.forward(fake_query)
        else:
            return self.netD.forward(input_concat)

    def forward(self, label, inst, image, feat, infer=False):
        # Encode Inputs
        input_label, inst_map, real_image, feat_map = self.encode_input(label, inst, image, feat)

        # Fake Generation
        if self.use_features:
            if not self.opt.load_features:
                if self.opt.label_feat: # concatenate label features
                    if self.opt.faster:
                        feat_map = self.netE.forward_fast(real_image, label.data) #.cuda())
                    else:
                        feat_map = self.netE.forward(real_image, label.data) #.cuda())
                else: # concatenate instance features
                    if self.opt.faster:
                        feat_map = self.netE.forward_fast(real_image, inst_map)
                    else:
                        feat_map = self.netE.forward(real_image, inst_map)
            input_concat = torch.cat((input_label, feat_map), dim=1)
        else:
            input_concat = input_label

        fake_image = self.netG.forward(input_concat)

        # Fake Detection and Loss
        pred_fake_pool = self.discriminate(input_label, fake_image, use_pool=True)
        loss_D_fake = self.criterionGAN(pred_fake_pool, False)
        loss_D_fake = loss_D_fake.unsqueeze(0)


        # Real Detection and Loss
        pred_real = self.discriminate(input_label, real_image)
        loss_D_real = self.criterionGAN(pred_real, True)
        loss_D_real = loss_D_real.unsqueeze(0)


        # GAN loss (Fake Passability Loss)
        pred_fake = self.netD.forward(torch.cat((input_label, fake_image), dim=1))
        loss_G_GAN = self.criterionGAN(pred_fake, True)
        loss_G_GAN = loss_G_GAN.unsqueeze(0)


        # GAN feature matching loss
        loss_G_GAN_Feat = 0
        if not self.opt.no_ganFeat_loss:
            feat_weights = 4.0 / (self.opt.n_layers_D + 1)
            D_weights = 1.0 / self.opt.num_D
            for i in range(self.opt.num_D):
                for j in range(len(pred_fake[i])-1):
                    loss_G_GAN_Feat += D_weights * feat_weights * \
                        self.criterionFeat(pred_fake[i][j], pred_real[i][j].detach()) * self.opt.lambda_feat
            loss_G_GAN_Feat = loss_G_GAN_Feat.unsqueeze(0)

        # VGG feature matching loss
        loss_G_VGG = 0
        loss_G_style_VGG = 0
        if not self.opt.no_style_loss:
            loss_G_VGG, loss_G_style_VGG = self.criterionVGG(fake_image, real_image)
            # print('VGG:', loss_G_VGG, loss_G_style_VGG)
            loss_G_VGG *= self.opt.lambda_feat
            loss_G_style_VGG *= self.opt.lambda_style
            loss_G_style_VGG = loss_G_style_VGG.unsqueeze(0)
        elif not self.opt.no_vgg_loss:
            loss_G_VGG = self.criterionVGG(fake_image, real_image) * self.opt.lambda_feat
            loss_G_VGG = loss_G_VGG.unsqueeze(0)
            loss_G_VGG = loss_G_VGG.unsqueeze(0)


        # L1 reconstruction loss
        loss_G_recon = 0
        if not self.opt.no_recon_loss:
            loss_G_recon = self.criterionRecon(fake_image, real_image) * self.opt.lambda_recon # loss(input, target)
            loss_G_recon = loss_G_recon.unsqueeze(0)

        # Only return the fake_B image if necessary to save BW
        return [ self.loss_filter( loss_G_GAN, loss_G_GAN_Feat, loss_G_VGG, loss_G_style_VGG, loss_G_recon, loss_D_real, loss_D_fake ), None if not infer else fake_image ]

    def condition_inference(self, condition_label, reference_label, condition_inst, reference_inst, condition_img, reference_img, swapID):
        # Encode features for reference image
        # reference_img = Variable(reference_img.data.cuda())
        input_label, _, reference_img, _ = self.encode_input(reference_label, reference_inst, reference_img)
        if torch.__version__.startswith('0.4'):
            with torch.no_grad():
                reference_feat_map = self.netE.forward(reference_img, reference_label.data) #.cuda())
        else:
            reference_label.requires_grad = False
            reference_img.requires_grad = False
            reference_feat_map = self.netE.forward(reference_img, reference_label.data) #.cuda())

        if condition_img is not None:
            # Encode features for condtioned image
            input_label, _, condition_img, _ = self.encode_input(condition_label, condition_inst, condition_img)
            if torch.__version__.startswith('0.4'):
                with torch.no_grad():
                    condition_feat_map = self.netE.forward(condition_img, condition_label.data) #.cuda())
            else:
                condition_label.requires_grad = False
                condition_img.requires_grad = False
                condition_feat_map = self.netE.forward(condition_img, condition_label.data) #.cuda())
            print(condition_feat_map.requires_grad, reference_feat_map.requires_grad)

            # Overwrite the feature of swapID in condition_feat by reference_feat
            # self.swap_features(swapID, condition_feat_map, reference_feat_map, condition_label.data.cuda(), reference_label.data.cuda())
            self.swap_features(swapID, condition_feat_map, reference_feat_map, condition_label.data, reference_label.data)
            # CHECK condition_feat_map successfully swapped
            input_concat = torch.cat((input_label, condition_feat_map), dim=1)
        else:
            input_concat = torch.cat((input_label, reference_feat_map), dim=1)


        # Fake Generation

        if torch.__version__.startswith('0.4'):
            with torch.no_grad():
                fake_image = self.netG.forward(input_concat)
        else:
            fake_image = self.netG.forward(input_concat)
        return fake_image

    def inference_given_feature (self, label, inst, features, random=False, from_avg=False):
        # Encode Inputs
        input_label, inst_map, _, _ = self.encode_input(Variable(label), Variable(inst), infer=True)

        # Fake Generation
        if self.use_features:
            # broadcast features into feautre maps according to label
            # feat_map = self.broadcast_features(features, label.data.cuda(), random, from_avg)
            feat_map = self.broadcast_features(features, label.data, random, from_avg)
            input_concat = torch.cat((input_label, feat_map), dim=1)
        else:
            input_concat = input_label

        if (torch.__version__.startswith('0.4')) or (torch.__version__.startswith('0.5')):
            with torch.no_grad():
                fake_image = self.netG.forward(input_concat)
        else:
            fake_image = self.netG.forward(input_concat)
        return fake_image

    def inference(self, label, inst):
        # Encode Inputs
        input_label, inst_map, _, _ = self.encode_input(Variable(label), Variable(inst), infer=True)

        # Fake Generation
        if self.use_features:
            # sample clusters from precomputed features
            feat_map = self.sample_features(inst_map)
            input_concat = torch.cat((input_label, feat_map), dim=1)
        else:
            input_concat = input_label

        if torch.__version__.startswith('0.4'):
            with torch.no_grad():
                fake_image = self.netG.forward(input_concat)
        else:
            fake_image = self.netG.forward(input_concat)
        return fake_image

    def broadcast_features(self, features_dict, label_map, random=False, from_avg=False):
        label_map_np = label_map.cpu().numpy().astype(int)
        feat_map = self.Tensor(label_map.size()[0], self.opt.feat_num, label_map.size()[2], label_map.size()[3])
        for i in np.unique(label_map_np):
            label = i if i < 1000 else i//1000
            if label in features_dict:
                if label == 6 and random:
                    # print(features_dict[label].shape)
                    feat = self.get_z_random(features_dict[label].shape[-1]) #CHECK
                    feat = feat.view(-1, features_dict[label].shape[-1])
                else:
                    # print(features_dict[label].shape)
                    feat = features_dict[label]
                idx = (label_map == int(i)).nonzero()
                for k in range(self.opt.feat_num):
                    feat_map[idx[:,0], idx[:,1] + k, idx[:,2], idx[:,3]] = feat[0, k]
            else:
                if from_avg:
                    feat = np.expand_dims(self.avg_features[label, :], axis=0)
                    idx = (label_map == int(i)).nonzero()
                    for k in range(self.opt.feat_num):
                        feat_map[idx[:,0], idx[:,1] + k, idx[:,2], idx[:,3]] = feat[0, k]
                else:
                    pass
        if self.opt.data_type==16:
            feat_map = feat_map.half()
        return feat_map

    def set_avg_features(self, avg_features):
        self.avg_features = avg_features

    def sample_features(self, inst):
        # read precomputed feature clusters
        cluster_path = os.path.join(self.opt.checkpoints_dir, self.opt.name, self.opt.cluster_path)
        features_clustered = np.load(cluster_path).item()

        # randomly sample from the feature clusters
        inst_np = inst.cpu().numpy().astype(int)
        feat_map = self.Tensor(inst.size()[0], self.opt.feat_num, inst.size()[2], inst.size()[3])
        for i in np.unique(inst_np):
            label = i if i < 1000 else i//1000
            if label in features_clustered:
                feat = features_clustered[label]
                cluster_idx = np.random.randint(0, feat.shape[0])

                idx = (inst == int(i)).nonzero()
                for k in range(self.opt.feat_num):
                    feat_map[idx[:,0], idx[:,1] + k, idx[:,2], idx[:,3]] = feat[cluster_idx, k]
        if self.opt.data_type==16:
            feat_map = feat_map.half()
        return feat_map

    def simple_encode_features(self, image, inst): # my implementation
        # image = Variable(image.cuda(), volatile=True)
        image = Variable(image, volatile=True)
        feat_num = self.opt.feat_num
        h, w = inst.size()[2], inst.size()[3]
        block_num = 32
        if (torch.__version__.startswith('0.4')) or (torch.__version__.startswith('0.5')):
            with torch.no_grad():
                feat_map = self.netE.forward(image, inst) #.cuda())
        else:
            feat_map = self.netE.forward(image, inst) #.cuda())
        inst_np = inst.cpu().numpy().astype(int)
        feature = {}
        for i in np.unique(inst_np):
            indices = (inst == int(i)).nonzero() # n (row)  x 4 (col) matrix, for example: [[0,0,0,0],[0,0,0,1],....[0,0,255,254],[0,0,255,255]]
            if indices.size()[0] > 0: # Check indices is not empty array
                feature[i] = feat_map[indices[0,0], :, indices[0,2], indices[0,3]].unsqueeze(0) # add additional dimension for batch
        return feature

    def encode_features(self, image, inst):
        # image = Variable(image.cuda(), volatile=True)
        image = Variable(image, volatile=True)
        feat_num = self.opt.feat_num
        h, w = inst.size()[2], inst.size()[3]
        block_num = 32
        feat_map = self.netE.forward(image, inst) #.cuda())
        inst_np = inst.cpu().numpy().astype(int)
        feature = {}
        for i in range(self.opt.label_nc):
            feature[i] = np.zeros((0, feat_num+1))
        for i in np.unique(inst_np):
            label = i if i < 1000 else i//1000
            idx = (inst == int(i)).nonzero()
            num = idx.size()[0]
            idx = idx[num//2,:] # // integer divison
            val = np.zeros((1, feat_num+1))
            for k in range(feat_num):
                val[0, k] = feat_map[idx[0], idx[1] + k, idx[2], idx[3]].item() #.data[0]
            val[0, feat_num] = float(num) / (h * w // block_num)
            feature[label] = np.append(feature[label], val, axis=0)
        return feature

    def swap_features(self, swapID, condition_feat_map, reference_feat_map, condition_label, reference_label):
        # return
        inst_list = np.unique(condition_label.cpu().numpy().astype(int))
        print(inst_list)
        assert(swapID in inst_list)
        inst_list = np.unique(reference_label.cpu().numpy().astype(int))
        print(inst_list)
        if swapID not in inst_list:
            equivID = self.get_equiv_ID(swapID)
        else:
            equivID = swapID
        print(equivID)
        assert(equivID in inst_list)

        condition_indices = (condition_label[0:1] == int(swapID)).nonzero() # n x 4: n = number of non-zero elements, 4 = input has 4 dimensions
        reference_indices = (reference_label[0:1] == int(equivID)).nonzero() # n x 4: n = number of non-zero elements, 4 = input has 4 dimensions

        all_indices = (torch.t(condition_indices))
        sample_indices = reference_indices[0,:]
        condition_feat_map[all_indices[0], :, all_indices[2], all_indices[3]] = reference_feat_map[sample_indices[0], :, sample_indices[2], sample_indices[3]]


    def get_equiv_ID(self, ID):
        # if ID == 7: # dress
        #     return 1 # T-shirt
        if ID == 5: # blouse
            return 1 # T-shirt
        elif ID == 22: # sweater
            return 1 # T-shirt
        # if ID == 1: # T-shirt
        #     return 5 # blouse
        # if ID == 1: # T-shirt
        #     return 7 # dress
        # elif ID == 12: # leggings
        #     return 13 # pants

    def get_edges(self, t):
        # edge = torch.cuda.ByteTensor(t.size()).zero_()
        edge = torch.ByteTensor(t.size()).zero_()
        edge[:,:,:,1:] = edge[:,:,:,1:] | (t[:,:,:,1:] != t[:,:,:,:-1])
        edge[:,:,:,:-1] = edge[:,:,:,:-1] | (t[:,:,:,1:] != t[:,:,:,:-1])
        edge[:,:,1:,:] = edge[:,:,1:,:] | (t[:,:,1:,:] != t[:,:,:-1,:])
        edge[:,:,:-1,:] = edge[:,:,:-1,:] | (t[:,:,1:,:] != t[:,:,:-1,:])
        if self.opt.data_type==16:
            return edge.half()
        else:
            return edge.float()

    def save(self, which_epoch):
        self.save_network(self.netG, 'G', which_epoch, self.gpu_ids)
        self.save_network(self.netD, 'D', which_epoch, self.gpu_ids)
        if self.gen_features:
            self.save_network(self.netE, 'E', which_epoch, self.gpu_ids)

    def update_fixed_params(self):
        # after fixing the global generator for a number of iterations, also start finetuning it
        params = list(self.netG.parameters())
        if self.gen_features:
            params += list(self.netE.parameters())
        self.optimizer_G = torch.optim.Adam(params, lr=self.opt.lr, betas=(self.opt.beta1, 0.999))
        if self.opt.verbose:
            print('------------ Now also finetuning global generator -----------')

    def update_learning_rate(self):
        lrd = self.opt.lr / self.opt.niter_decay
        lr = self.old_lr - lrd
        for param_group in self.optimizer_D.param_groups:
            param_group['lr'] = lr
        for param_group in self.optimizer_G.param_groups:
            param_group['lr'] = lr
        if self.opt.verbose:
            print('update learning rate: %f -> %f' % (self.old_lr, lr))
        self.old_lr = lr

    def set_continue_learning_rate(self, lr):
        for param_group in self.optimizer_D.param_groups:
            param_group['lr'] = lr
        for param_group in self.optimizer_G.param_groups:
            param_group['lr'] = lr
        print('set conitnue learning rate: %f' % (lr))
        self.old_lr = lr

class InferenceModel(Pix2PixHDModel):
    def forward(self, inp):
        label, inst = inp
        return self.inference(label, inst)
