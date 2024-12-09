import numpy as np
import torch as tc
#tc.set_default_tensor_type(tc.FloatTensor)
import torch.nn as nn
from util import rotate

tc.set_default_dtype(tc.float32)  # Set the default tensor dtype
#tc.set_default_device('cuda:0')  # Set the default device to CPU (or 'cuda:0' for GPU)


class PPM(nn.Module):
 
    def __init__(self, dev, model_self_absorption, lac, grid_concentration, p, n_element, n_lines, 
                 mass_attenuation_cross_section_FL, detected_fl_unit_concentration, n_line_group_each_element,
                 sample_height_n, minibatch_size, sample_size_n, sample_size_cm,          
                 probe_energy, incident_probe_intensity, model_probe_attenuation, probe_attCS_ls,
                 theta, signal_attenuation_factor,
                 n_det, P_minibatch, det_dia_cm, det_from_sample_cm, det_solid_angle_ratio):
        """
        Initialize the attributes of PPM. 
        """
        super(PPM, self).__init__() # inherit the __init__ from nn.Module.
        self.dev = dev # device
        self.model_self_absorption = model_self_absorption # include self absorption in the forward model
        self.lac = lac
        self.grid_concentration = grid_concentration
        self.p = p  # indicate which minibatch to calculate the gradient
        self.n_element = n_element
        self.n_lines = n_lines

        self.mass_attenuation_cross_section_FL = mass_attenuation_cross_section_FL.to(self.dev)
        self.detected_fl_unit_concentration = detected_fl_unit_concentration.to(self.dev)
        self.n_line_group_each_element = n_line_group_each_element.to(self.dev)
        
        self.sample_height_n = sample_height_n
        self.minibatch_size = minibatch_size
        self.sample_size_n = sample_size_n
        self.sample_size_cm = sample_size_cm
        self.dia_len_n = int(1.2 * (self.sample_height_n**2 + self.sample_size_n**2 + self.sample_size_n**2)**0.5)
        self.n_voxel_minibatch = self.minibatch_size * self.sample_size_n
        self.n_voxel = self.sample_height_n * self.sample_size_n**2 
        
        self.xp = self.init_xp() # initialize the values of the minibatch      
        self.probe_energy = probe_energy  
        self.incident_probe_intensity = incident_probe_intensity
        self.model_probe_attenuation = model_probe_attenuation
        self.probe_attCS_ls =  probe_attCS_ls # the mass attenuation cross section of probe (2nd row in Table 5.3.1) as a list 
        self.probe_before_attenuation_flat = self.init_probe()        
              
        self.theta = theta
        self.signal_attenuation_factor = signal_attenuation_factor # constant
             
        self.n_det = n_det
        self.P_minibatch = P_minibatch 
        self.det_dia_cm = det_dia_cm
        self.det_from_sample_cm = det_from_sample_cm
        self.SA_theta = self.init_SA_theta()
        self.det_solid_angle_ratio = det_solid_angle_ratio
        
        
    def init_xp(self):
        """
        Initialize self.x with the tensor of the saved intermediate reconstructing results (n_element, minibatch_size, n_y)
        """
        ## set grid_concentration[:, N(this_minibatch_st): N(this_minibatch_end), :, :] to be the model parameters
        return nn.Parameter(self.grid_concentration[:, self.minibatch_size * self.p // self.sample_size_n : self.minibatch_size*(self.p+1) // self.sample_size_n, :, :])
    

    def init_SA_theta(self):
        if self.model_self_absorption == True:
            voxel_idx_offset = self.p * self.n_voxel_minibatch
            
            # clamp the index after substrcting the offset, so that all 0 indicies remains 0 (becomes negative if without clamping, and cause errors)
            att_exponent = tc.stack([self.lac[:,:, tc.clamp((self.P_minibatch[m,0] - voxel_idx_offset), 0, self.n_voxel_minibatch).to(dtype=tc.long),\
                                              self.P_minibatch[m,1].to(dtype=tc.long)]\
                                    * self.P_minibatch[m,2].repeat(self.n_element, self.n_lines, 1) for m in range(self.n_det)])
            
            # lac, dim = [n_element, n_lines, n_voxel_minibatch, n_voxel]
            # att_exponent, dim = [n_det, n_element, n_lines, n_source, n_dia_length]
            
            ## summing over the attenation exponent contributed by all intersecting voxels, dim = (n_det, n_element, n_lines, n_voxel_minibatch(FL source))
            att_exponent_voxel_sum = tc.sum(att_exponent.view(self.n_det, self.n_element, self.n_lines, self.n_voxel_minibatch, self.dia_len_n), axis=-1)

            ## calculate the attenuation caused by all elements, dim = (n_det, n_lines, n_voxel_minibatch(FL source)), and then take the average over n_det FL paths
            SA_theta =  tc.mean(tc.exp(-tc.sum(att_exponent_voxel_sum, axis=1)), axis=0)           
            # SA_theta, dim = (n_lines, n_source)
            #print(f'SA_theta:{SA_theta}')
        else:
            SA_theta = 1
        
        return SA_theta

    def init_probe(self):       
        probe_before_attenuation = self.incident_probe_intensity * tc.ones(self.minibatch_size, self.sample_size_n, device = self.dev)
        return probe_before_attenuation.view(self.n_voxel_minibatch)
    
    
    def forward(self): 
        """
        Forward propagation.
        """      
        
        ### 1: Calculate the map of attenuation and transmission ###   
        # create a array to store the initilized updating parameters
        concentration_map_minibatch = self.xp ## dimension = [C, N(this minibatch), H, W]
        
        # Rotate the layers in the minibatch
        concentration_map_minibatch_rot = rotate(concentration_map_minibatch, self.theta, self.dev)
        concentration_map_minibatch_rot = tc.reshape(concentration_map_minibatch_rot, (self.n_element, self.minibatch_size, self.sample_size_n))
        
        ## Calculate the attenuation of the probe
        # Calculate the expoenent of attenuation of each voxel in the batch. (The atteuation before the probe enters each voxel.)
        att_exponent_acc_map = tc.zeros((self.minibatch_size, self.sample_size_n+1), device=self.dev)
        
        fl_map_tot_flat_theta = tc.zeros((self.n_lines, self.n_voxel_minibatch), device=self.dev)
        concentration_map_minibatch_rot_flat = concentration_map_minibatch_rot.view(self.n_element, self.n_voxel_minibatch)
        line_idx = 0
        for j in range(self.n_element):
            ## step 1: calculate the attenuation exponent at each voxel
            if self.model_probe_attenuation == True:
                lac_single = concentration_map_minibatch_rot[j] * self.probe_attCS_ls[j]
                lac_acc = tc.cumsum(lac_single, axis=1) # dim = (minibatch_size, sample_size_n)
                lac_acc = tc.cat((tc.zeros((self.minibatch_size, 1), device=self.dev), lac_acc), dim = 1) # dim = (minibatch_size, sample_size_n + 1)
                att_exponent_acc = lac_acc * (self.sample_size_cm / self.sample_size_n)    
                att_exponent_acc_map += att_exponent_acc
            
            else:
                att_exponent_acc_map = tc.zeros(self.minibatch_size, self.sample_size_n+1).to(self.dev)
            ## step 2: calculate the fluorescence signal generated at each voxel
            fl_unit = self.detected_fl_unit_concentration[line_idx:line_idx + self.n_line_group_each_element[j]]            
            ## FL signal over the current elemental lines for each voxel
            fl_map = tc.stack([concentration_map_minibatch_rot_flat[j] * fl_unit_single_line for fl_unit_single_line in fl_unit])            
            fl_map_tot_flat_theta[line_idx:line_idx + self.n_line_group_each_element[j],:] = fl_map            
            line_idx = line_idx + len(fl_unit)
            
        attenuation_map_theta_flat = tc.exp(-(att_exponent_acc_map[:,:-1])).view(self.n_voxel_minibatch)
#         transmission_theta = tc.exp(-att_exponent_acc_map[:,-1])
        transmission_att_exponent_theta = att_exponent_acc_map[:,-1]
             
        #### 4: Create XRF, XRT data ####           
        probe_after_attenuation_theta = self.probe_before_attenuation_flat * attenuation_map_theta_flat 
        # fl_signal_SA_theta, dim = (n_lines, n_minibatch)
        fl_signal_SA_theta = tc.unsqueeze(probe_after_attenuation_theta, dim=0) * fl_map_tot_flat_theta * self.SA_theta  
        fl_signal_SA_theta = fl_signal_SA_theta.view(self.n_lines, self.minibatch_size, self.sample_size_n)
        fl_signal_SA_theta = tc.sum(fl_signal_SA_theta, axis=-1)
        
        fl_signal_SA_theta = fl_signal_SA_theta * self.det_solid_angle_ratio * self.signal_attenuation_factor
         
        output1 = fl_signal_SA_theta
#         output2 = self.incident_probe_intensity * transmission_theta
        output2 = transmission_att_exponent_theta

        return output1, output2
    
    