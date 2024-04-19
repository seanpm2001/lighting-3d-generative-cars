# SPDX-FileCopyrightText: Copyright (c) 2021-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Loss functions."""

import numpy as np
import torch
from torch_utils import training_stats
from torch_utils.ops import conv2d_gradfix
from torch_utils.ops import upfirdn2d
from training.dual_discriminator import filtered_resizing

#----------------------------------------------------------------------------

class Loss:
    def accumulate_gradients(self, phase, real_img, specular_envmap, diffuse_envmap, real_c, gen_z, gen_c, gain, cur_nimg): # to be overridden by subclass
        raise NotImplementedError()

#----------------------------------------------------------------------------

class StyleGAN2Loss(Loss):
    def __init__(self, device, G, D, augment_pipe=None, r1_gamma=10, style_mixing_prob=0, pl_weight=0, pl_batch_shrink=2, pl_decay=0.01, pl_no_weight_grad=False, blur_init_sigma=0, blur_fade_kimg=0, r1_gamma_init=0, r1_gamma_fade_kimg=0, neural_rendering_resolution_initial=64, neural_rendering_resolution_final=None, neural_rendering_resolution_fade_kimg=0, gpc_reg_fade_kimg=1000, gpc_reg_prob=None, dual_discrimination=False, filter_mode='antialiased'):
        super().__init__()
        self.device             = device
        self.G                  = G
        self.D                  = D
        self.augment_pipe       = augment_pipe
        self.r1_gamma           = r1_gamma
        self.style_mixing_prob  = style_mixing_prob
        self.pl_weight          = pl_weight
        self.pl_batch_shrink    = pl_batch_shrink
        self.pl_decay           = pl_decay
        self.pl_no_weight_grad  = pl_no_weight_grad
        self.pl_mean            = torch.zeros([], device=device)
        self.blur_init_sigma    = blur_init_sigma
        self.blur_fade_kimg     = blur_fade_kimg
        self.r1_gamma_init      = r1_gamma_init
        self.r1_gamma_fade_kimg = r1_gamma_fade_kimg
        self.neural_rendering_resolution_initial = neural_rendering_resolution_initial
        self.neural_rendering_resolution_final = neural_rendering_resolution_final
        self.neural_rendering_resolution_fade_kimg = neural_rendering_resolution_fade_kimg
        self.gpc_reg_fade_kimg = gpc_reg_fade_kimg
        self.gpc_reg_prob = gpc_reg_prob
        self.dual_discrimination = dual_discrimination
        self.filter_mode = filter_mode
        self.resample_filter = upfirdn2d.setup_filter([1,3,3,1], device=device)
        self.blur_raw_target = True
        assert self.gpc_reg_prob is None or (0 <= self.gpc_reg_prob <= 1)

    def get_annealing(self, cur_nimg, start, end):
        return np.clip((cur_nimg - start) / (end - start), 0, 1)

    def run_G(self, z, c, specular_envmap, diffuse_envmap, swapping_prob, neural_rendering_resolution, update_emas=False):
        if swapping_prob is not None:
            c_swapped = torch.roll(c.clone(), 1, 0)
            c_gen_conditioning = torch.where(torch.rand((c.shape[0], 1), device=c.device) < swapping_prob, c_swapped, c)
        else:
            c_gen_conditioning = torch.zeros_like(c)

        z_obj = z[:, :512]
        z_cam = z[:, 512:1024]
        ws = self.G.mapping(z_obj, c_gen_conditioning, update_emas=update_emas)
        # ws_cam = self.G.cam_mapping(None, c[:, :16], update_emas=update_emas)
        if self.style_mixing_prob > 0:
            with torch.autograd.profiler.record_function('style_mixing'):
                cutoff = torch.empty([], dtype=torch.int64, device=ws.device).random_(1, ws.shape[1])
                cutoff = torch.where(torch.rand([], device=ws.device) < self.style_mixing_prob, cutoff, torch.full_like(cutoff, ws.shape[1]))
                ws[:, cutoff:] = self.G.mapping(torch.randn_like(z), c, update_emas=False)[:, cutoff:]
        gen_output = self.G.synthesis(ws, c, specular_envmap, diffuse_envmap, z_cam=z_cam, neural_rendering_resolution=neural_rendering_resolution, update_emas=update_emas)
        return gen_output, ws

    def run_D(self, img, c, blur_sigma=0, blur_sigma_raw=0, update_emas=False):
        blur_size = np.floor(blur_sigma * 3)
        if blur_size > 0:
            with torch.autograd.profiler.record_function('blur'):
                f = torch.arange(-blur_size, blur_size + 1, device=img['sr_image'].device).div(blur_sigma).square().neg().exp2()
                img['sr_image'] = upfirdn2d.filter2d(img['sr_image'], f / f.sum())

        if self.augment_pipe is not None:
            augmented_pair = self.augment_pipe(torch.cat([img['sr_image'],
                                                    torch.nn.functional.interpolate(img['lr_image'], size=img['sr_image'].shape[2:], mode='bilinear', antialias=True)],
                                                    dim=1))
            img['sr_image'] = augmented_pair[:, :img['sr_image'].shape[1]]
            img['lr_image'] = torch.nn.functional.interpolate(augmented_pair[:, img['sr_image'].shape[1]:], size=img['lr_image'].shape[2:], mode='bilinear', antialias=True)

        logits = self.D(img, c, update_emas=update_emas)
        return logits

    def accumulate_gradients(self, phase, real_img, specular_envmap, diffuse_envmap, real_c, gen_z, gen_c, gain, cur_nimg):
        assert phase in ['Gmain', 'Greg', 'Gboth', 'Dmain', 'Dreg', 'Dboth']
        if self.G.rendering_kwargs.get('density_reg', 0) == 0:
            phase = {'Greg': 'none', 'Gboth': 'Gmain'}.get(phase, phase)
        if self.r1_gamma == 0:
            phase = {'Dreg': 'none', 'Dboth': 'Dmain'}.get(phase, phase)
        blur_sigma = max(1 - cur_nimg / (self.blur_fade_kimg * 1e3), 0) * self.blur_init_sigma if self.blur_fade_kimg > 0 else 0
        r1_gamma = self.r1_gamma

        alpha = min(cur_nimg / (self.gpc_reg_fade_kimg * 1e3), 1) if self.gpc_reg_fade_kimg > 0 else 1
        swapping_prob = (1 - alpha) * 1 + alpha * self.gpc_reg_prob if self.gpc_reg_prob is not None else None

        if self.neural_rendering_resolution_final is not None:
            alpha = min(cur_nimg / (self.neural_rendering_resolution_fade_kimg * 1e3), 1)
            neural_rendering_resolution = int(np.rint(self.neural_rendering_resolution_initial * (1 - alpha) + self.neural_rendering_resolution_final * alpha))
        else:
            neural_rendering_resolution = self.neural_rendering_resolution_initial

        size = [neural_rendering_resolution, neural_rendering_resolution]
        real_img_raw = filtered_resizing(real_img, size=size, f=self.resample_filter, filter_mode=self.filter_mode)

        if self.blur_raw_target:
            blur_size = np.floor(blur_sigma * 3)
            if blur_size > 0:
                f = torch.arange(-blur_size, blur_size + 1, device=real_img_raw.device).div(blur_sigma).square().neg().exp2()
                real_img_raw = upfirdn2d.filter2d(real_img_raw, f / f.sum())
        
        real_img = {'sr_image': real_img, 'lr_image': real_img_raw}

        # Gmain: Maximize logits for generated images.
        if phase in ['Gmain', 'Gboth']:
            with torch.autograd.profiler.record_function('Gmain_forward'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c, specular_envmap, diffuse_envmap, swapping_prob=swapping_prob, neural_rendering_resolution=neural_rendering_resolution)
                gen_logits = self.run_D(gen_img, gen_c, blur_sigma=blur_sigma)
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Gmain = torch.nn.functional.softplus(-gen_logits)
                training_stats.report('Loss/G/loss', loss_Gmain)

        # Base Color Regularization
        if phase in ['Greg', 'Gboth'] and self.G.rendering_kwargs.get('base_color_reg', 0) > 0:
            ws = self.G.mapping(gen_z[:, :512], torch.zeros_like(gen_c), update_emas=False)
            bbox_min = torch.tensor(self.G.rendering_kwargs['bbox_min'], dtype=ws.dtype, device=ws.device).repeat(ws.shape[0], 1000, 1)
            bbox_max = torch.tensor(self.G.rendering_kwargs['bbox_max'], dtype=ws.dtype, device=ws.device).repeat(ws.shape[0], 1000, 1)
            initial_coordinates = torch.rand((ws.shape[0], 1000, 3), device=ws.device) * (bbox_max - bbox_min) + bbox_min

            perturbed_coordinates = initial_coordinates + torch.randn_like(initial_coordinates) * self.G.rendering_kwargs['base_color_reg_p_dist']
            all_coordinates = torch.cat([initial_coordinates, perturbed_coordinates], dim=1)
            base_color = self.G.sample_mixed(all_coordinates, torch.randn_like(all_coordinates), ws, update_emas=False)['rgb']
            base_color_initial = base_color[:, :base_color.shape[1]//2]
            base_color_perturbed = base_color[:, base_color.shape[1]//2:]

            basecolor_loss = torch.nn.functional.l1_loss(base_color_initial, base_color_perturbed) * self.G.rendering_kwargs['base_color_reg']
            basecolor_loss.mul(gain).backward()
            training_stats.report('Loss/Greg/basecolor', basecolor_loss)


        # Density Regularization
        if phase in ['Greg', 'Gboth'] and self.G.rendering_kwargs.get('density_reg', 0) > 0:

            ws = self.G.mapping(gen_z[:, :512], torch.zeros_like(gen_c), update_emas=False)

            bbox_min = torch.tensor(self.G.rendering_kwargs['bbox_min'], dtype=ws.dtype, device=ws.device).repeat(ws.shape[0], 1000, 1)
            bbox_max = torch.tensor(self.G.rendering_kwargs['bbox_max'], dtype=ws.dtype, device=ws.device).repeat(ws.shape[0], 1000, 1)

            initial_coordinates = torch.rand((ws.shape[0], 1000, 3), device=ws.device) * (bbox_max - bbox_min) + bbox_min

            perturbed_coordinates = initial_coordinates + torch.randn_like(initial_coordinates) * self.G.rendering_kwargs['density_var_reg_p_dist']
            all_coordinates = torch.cat([initial_coordinates, perturbed_coordinates], dim=1)
            alpha = self.G.sample_mixed(all_coordinates, torch.randn_like(all_coordinates), ws, update_emas=False)['sigma']
            alpha_initial = alpha[:, :alpha.shape[1]//2]
            alpha_perturbed = alpha[:, alpha.shape[1]//2:]

            variance_alpha_loss = torch.nn.functional.l1_loss(alpha_initial, alpha_perturbed)
            variance_alpha_loss = variance_alpha_loss.mul(gain)

            avg_alpha = alpha_initial.mean(dim=[1, 2])
            min_alpha_loss = torch.nn.functional.l1_loss(avg_alpha, torch.max(avg_alpha, torch.ones_like(avg_alpha) * self.G.rendering_kwargs['density_min_reg_target']))
            min_alpha_loss = min_alpha_loss.mul(gain)

            total_loss = (variance_alpha_loss + min_alpha_loss) * self.G.rendering_kwargs['density_reg']
            total_loss.backward()
            training_stats.report('Loss/Greg/density', total_loss)

        # Dmain: Minimize logits for generated images.
        loss_Dgen = 0
        if phase in ['Dmain', 'Dboth']:
            with torch.autograd.profiler.record_function('Dgen_forward'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c, specular_envmap, diffuse_envmap, swapping_prob=swapping_prob, neural_rendering_resolution=neural_rendering_resolution, update_emas=True)
                gen_logits = self.run_D(gen_img, gen_c, blur_sigma=blur_sigma, update_emas=True)
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Dgen = torch.nn.functional.softplus(gen_logits)
            with torch.autograd.profiler.record_function('Dgen_backward'):
                loss_Dgen.mean().mul(gain).backward()

        # Dmain: Maximize logits for real images.
        # Dr1: Apply R1 regularization.
        if phase in ['Dmain', 'Dreg', 'Dboth']:
            name = 'Dreal' if phase == 'Dmain' else 'Dr1' if phase == 'Dreg' else 'Dreal_Dr1'
            with torch.autograd.profiler.record_function(name + '_forward'):
                real_img_tmp_image = real_img['sr_image'].detach().requires_grad_(phase in ['Dreg', 'Dboth'])
                real_img_tmp_image_raw = real_img['lr_image'].detach().requires_grad_(phase in ['Dreg', 'Dboth'])
                real_img_tmp = {'sr_image': real_img_tmp_image, 'lr_image': real_img_tmp_image_raw}

                real_logits = self.run_D(real_img_tmp, real_c, blur_sigma=blur_sigma)
                training_stats.report('Loss/scores/real', real_logits)
                training_stats.report('Loss/signs/real', real_logits.sign())

                loss_Dreal = 0
                if phase in ['Dmain', 'Dboth']:
                    loss_Dreal = torch.nn.functional.softplus(-real_logits)
                    training_stats.report('Loss/D/loss', loss_Dgen + loss_Dreal)

                loss_Dr1 = 0
                if phase in ['Dreg', 'Dboth']:
                    if self.dual_discrimination:
                        with torch.autograd.profiler.record_function('r1_grads'), conv2d_gradfix.no_weight_gradients():
                            r1_grads = torch.autograd.grad(outputs=[real_logits.sum()], inputs=[real_img_tmp['sr_image'], real_img_tmp['lr_image']], create_graph=True, only_inputs=True)
                            r1_grads_image = r1_grads[0]
                            r1_grads_image_raw = r1_grads[1]
                        r1_penalty = r1_grads_image.square().sum([1,2,3]) + r1_grads_image_raw.square().sum([1,2,3])
                    else: # single discrimination
                        with torch.autograd.profiler.record_function('r1_grads'), conv2d_gradfix.no_weight_gradients():
                            r1_grads = torch.autograd.grad(outputs=[real_logits.sum()], inputs=[real_img_tmp['sr_image']], create_graph=True, only_inputs=True)
                            r1_grads_image = r1_grads[0]
                        r1_penalty = r1_grads_image.square().sum([1,2,3])
                    loss_Dr1 = r1_penalty * (r1_gamma / 2)
                    training_stats.report('Loss/r1_penalty', r1_penalty)
                    training_stats.report('Loss/D/reg', loss_Dr1)

            with torch.autograd.profiler.record_function(name + '_backward'):
                (loss_Dreal + loss_Dr1).mean().mul(gain).backward()

#----------------------------------------------------------------------------
