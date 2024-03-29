import argparse
import torch
import pandas as pd
from torchvision.datasets import MNIST, FashionMNIST, CIFAR10, CIFAR100
from torchvision import transforms
from torch.utils.data import DataLoader, ConcatDataset, random_split
import torch.distributed.rpc as rpc
from torch.autograd import Variable
import torch.distributed as dist
from torchvision.utils import make_grid
from torch import autograd
import torch.nn as nn
import imageio
from models_cifar10 import Generator, Discriminator
# from wgangp import WGANGP
import numpy as np
import os

import warnings
warnings.filterwarnings("ignore")

parser = argparse.ArgumentParser()



def _call_method(method, rref, *args, **kwargs):
    """helper for _remote_method()"""
    return method(rref.local_value(), *args, **kwargs)
def _remote_method(method, rref, *args, **kwargs):
    """
    executes method(*args, **kwargs) on the from the machine that owns rref
    very similar to rref.remote().method(*args, **kwargs), but method() doesn't have to be in the remote scope
    """
    args = [method, rref] + list(args)
    return rpc.rpc_sync(rref.owner(), _call_method, args=args, kwargs=kwargs)
def param_rrefs(module):
    """grabs remote references to the parameters of a module"""
    param_rrefs = []
    for param in module.parameters():
        param_rrefs.append(rpc.RRef(param))
    print(param_rrefs)
    return param_rrefs
def sum_of_layer(model_dicts, layer):
    """
    Sum of parameters of one layer for all models
    """
    layer_sum = model_dicts[0][layer]
    for i in range(1, len(model_dicts)):
        layer_sum += model_dicts[i][layer]
    return layer_sum
def average_model(model_dicts):
    """
    Average model by uniform weights
    """
    if len(model_dicts) == 1:
        return model_dicts[0]
    else:
        weights = 1/len(model_dicts)
        state_aggregate = model_dicts[0]
    for layer in state_aggregate:
        state_aggregate[layer] = weights*sum_of_layer(model_dicts, layer)
    return state_aggregate


class MDGANServer():
    """
    This is the class that encapsulates the functions that need to be run on the server side
    This is the main driver of the training procedure.
    """

    def __init__(self, client_rrefs, epochs, use_cuda, n_critic, **kwargs):
        # super(MDGANServer, self).__init__(**kwargs)
        print("number of epochs in initialization: ", epochs)
        self.epochs = epochs
        self.use_cuda = use_cuda
        self.device = torch.device("cuda:0" if use_cuda else "cpu")
        self.n_critic = n_critic
        self.latent_shape = [100, 1, 1]
        self._fixed_z = torch.randn(64, *self.latent_shape)
        self.images = []

        # keep a reference to the client
        self.client_rrefs = []
        for client_rref in client_rrefs:
            self.client_rrefs.append(client_rref)

        self.generator = Generator(100)
        self.G_opt = dist.optim.DistributedOptimizer(
            optim.Adam, param_rrefs(self.generator), lr=2e-4, betas=(0.5, 0.9), weight_decay=1e-6
        )
        # self.G_opt = torch.optim.Adam(self.generator.parameters(), lr=1e-4, betas=(0.5, 0.9))
        # register generator for each client.
        for client_rref in self.client_rrefs:
            client_rref.remote().register_G(rpc.RRef(self.generator)) 
        
        if self.use_cuda:
            self._fixed_z = self._fixed_z.cuda()
            # self.generator.cuda()


    def save_gif(self):

        imageio.mimsave('{}.gif'.format('mnist'), self.images)

    def fit(self):

        """
        E: the interval epochs to swap models
        """

        steps_per_epoch_list = []
        for client_rref in self.client_rrefs:
            steps_per_epoch_list.append(client_rref.remote().get_steps_number().to_here())
        self.steps_per_epoch = np.max(steps_per_epoch_list)

        for i in range(self.epochs):
            print("training epoch: ", i)
            loss_fut_list = []
            for client_rref in self.client_rrefs:
                print("D step update")
                loss_fut = client_rref.rpc_async().train_D()
                loss_fut_list.append(loss_fut)

            for id_ in range(len(self.client_rrefs)):
                loss_d, pen = loss_fut_list[id_].wait()
                print("LOSS D: ", loss_d, pen)

            if self.epochs % self.n_critic == 0:
                grid = make_grid(self.generator(self._fixed_z), normalize=True)
                grid = np.transpose(grid.numpy(), (1, 2, 0))
                self.images.append(grid)


            if i % self.n_critic == 0:
                for i in range(self.steps_per_epoch):
                    with dist.autograd.context() as G_context:
                        loss_g_list = []
                        for client_rref in self.client_rrefs:
                            print("G step update")
                            loss_g_list.append(client_rref.rpc_async().loss_G())

                        loss_accumulated = loss_g_list[0].wait()
                        for j in range(1, len(loss_g_list)):
                            loss_accumulated += loss_g_list[j].wait()
                        
                        dist.autograd.backward(G_context, [loss_accumulated])
                        self.G_opt.step(G_context)
        self.save_gif()
            


class MDGANClient():
    """
    This is the class that encapsulates the functions that need to be run on the client side
    Despite the name, this source code only needs to reside on the server and will be executed via RPC.
    """

    def __init__(self, dataset, epochs, use_cuda, batch_size, **kwargs):
        print("number of epochs in initialization: ", epochs)
        print("initalized dataset: ", dataset)
        self.counter = 0
        self.epochs = epochs
        self.latent_shape = [100, 1, 1]
        self.use_cuda = use_cuda
        self.batch_size = batch_size
        self.device = torch.device("cuda:0" if use_cuda else "cpu")

       
        self.steps_per_epoch = 10
        print("steps_per_epoch: ", self.steps_per_epoch)



        self.discriminator = Discriminator()
        chosen_number = np.random.randint(4)
        print("chosen number: ", chosen_number)
        if chosen_number == 1:
            self.discriminator.apply(self.discriminator_weight_init_xavier)
        if chosen_number == 2:
            self.discriminator.apply(self.discriminator_weight_init_normal)
        if chosen_number == 3:
            self.discriminator.apply(self.discriminator_weight_init_uniform)

        if self.use_cuda:
            self.discriminator.cuda()
        self.D_opt = torch.optim.Adam(self.discriminator.parameters(), lr=1e-4, betas=(0.5, 0.9))

    def discriminator_weight_init_xavier(self, m):
        if isinstance(m, nn.Conv2d):
            torch.nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                torch.nn.init.zeros_(m.bias)

    def discriminator_weight_init_normal(self, m):
        if isinstance(m, nn.Conv2d):
            torch.nn.init.normal_(m.weight)
            if m.bias is not None:
                torch.nn.init.zeros_(m.bias)        

    def discriminator_weight_init_uniform(self, m):
        if isinstance(m, nn.Conv2d):
            torch.nn.init.uniform_(m.weight)
            if m.bias is not None:
                torch.nn.init.zeros_(m.bias)

    def send_client_refs(self):
        """Send a reference to the discriminator (for future RPC calls) and the conditioner, transformer and steps/epoch"""
        return rpc.RRef(self.discriminator)


    def register_G(self, G_rref):
        """Receive a reference to the generator (for future RPC calls)"""
        self.G_rref = G_rref
        
    def sample_latent(self):
        z = Variable(torch.randn(self.batch_size, *self.latent_shape))
        # if self.use_cuda:
        #     z = z.cuda()
        return z

    def get_discriminator_weights(self):
        print("call in get_discriminator_weights: ")
        if next(self.discriminator.parameters()).is_cuda:
            return self.discriminator.cpu().state_dict()
        else:
            return self.discriminator.state_dict()
    def set_discriminator_weights(self, state_dict):
        print("call in set_discriminator_weights: ", self.device)
        self.discriminator.load_state_dict(state_dict)
        if self.device.type != 'cpu':
            print("set discriminator on cuda")
            self.discriminator.to(self.device)
    def reset_on_cuda(self):
        if self.device.type != 'cpu':
            self.discriminator.to(self.device)



    def get_steps_number(self):
        return self.steps_per_epoch

    def gradient_penalty(self, data, generated_data, gamma=10):
        batch_size = data.size(0)
        epsilon = torch.rand(batch_size, 1, 1, 1)
        epsilon = epsilon.expand_as(data)


        if self.use_cuda:
            epsilon = epsilon.cuda()

        interpolation = epsilon * data.data + (1 - epsilon) * generated_data.data
        interpolation = Variable(interpolation, requires_grad=True)

        if self.use_cuda:
            interpolation = interpolation.cuda()
        # print("shape of data", data.shape, generated_data.shape, interpolation.shape)
        interpolation_logits = self.discriminator(interpolation)
        grad_outputs = torch.ones(interpolation_logits.size())

        if self.use_cuda:
            grad_outputs = grad_outputs.cuda()

        gradients = autograd.grad(outputs=interpolation_logits,
                                  inputs=interpolation,
                                  grad_outputs=grad_outputs,
                                  create_graph=True,
                                  retain_graph=True)[0]

        gradients = gradients.view(batch_size, -1)
        # here add an epsilon to avoid sqrt error for number close to 0
        gradients_norm = torch.sqrt(torch.sum(gradients ** 2, dim=1) + 1e-12) 
        return gamma * ((gradients_norm - 1) ** 2).mean()

    def loss_G_data(self, generated_data):
        # generated_data = self.G_rref.remote().forward(self.sample_latent().cpu()).to_here()
        generated_data = generated_data.to(self.device)
        g_loss = -self.discriminator(generated_data).mean()
        return g_loss.cpu(), self.discriminator(generated_data).cpu().tolist()


    def loss_G(self):
        generated_data = self.G_rref.remote().forward(self.sample_latent().cpu()).to_here()
        generated_data = generated_data.to(self.device)
        g_loss = -self.discriminator(generated_data).mean()
        return g_loss.cpu()

    def evalute_D(self, testing_latent):
        if self.device.type != 'cpu':
            testing_latent = testing_latent.cuda()
            # print("discriminator output dimension: ", self.discriminator(testing_latent)[0:10])
            return self.discriminator(testing_latent).cpu().tolist()
        else:
            return self.discriminator(testing_latent).tolist()

    def train_D(self):
        '''

        '''
        
        print("training in free-rider")
            
        data = self.G_rref.remote().forward(self.sample_latent().cpu()).to_here()
        data = data.to(device=self.device)
        generated_data = torch.randint_like(data, 0, 1, device=self.device)

        grad_penalty = self.gradient_penalty(data, generated_data)
        d_loss = self.discriminator(generated_data).mean() - self.discriminator(data).mean() + grad_penalty
        self.D_opt.zero_grad()
        d_loss.backward()
        self.D_opt.step()

        self.counter += 1
        return d_loss.item(), grad_penalty.item()

        


def run(rank, world_size, ip, port, dataset, epochs, use_cuda, batch_size, n_critic):
    # set environment information
    os.environ["MASTER_ADDR"] = ip
    os.environ["MASTER_PORT"] = str(port)
    os.environ["WORLD_SIZE"] = str(world_size)
    os.environ["RANK"] = str(rank)
    print("number of epochs before initialization: ", epochs)
    print("world size: ", world_size, f"tcp://{ip}:{port}")
    if rank == 0:  # this is run only on the server side
        print("server")
        rpc.init_rpc(
            "server",
            rank=rank,
            world_size=world_size,
            backend=rpc.BackendType.TENSORPIPE,
            rpc_backend_options=rpc.TensorPipeRpcBackendOptions(
                num_send_recv_threads=8, rpc_timeout=120, init_method=f"tcp://{ip}:{port}"
            ),
        )
        print("after init_rpc")
        clients = []
        for worker in range(world_size-1):
            clients.append(rpc.remote("client"+str(worker+1), MDGANClient, kwargs=dict(dataset=dataset, epochs = epochs, use_cuda = use_cuda, batch_size=batch_size)))
            print("register remote client"+str(worker+1), clients[0])

        synthesizer = MDGANServer(clients, epochs, use_cuda, batch_size, n_critic)
        synthesizer.fit()

    elif rank != 0:
        print("client")
        rpc.init_rpc(
            "client"+str(rank),
            rank=rank,
            world_size=world_size,
            backend=rpc.BackendType.PROCESS_GROUP,
            rpc_backend_options=rpc.ProcessGroupRpcBackendOptions(
                num_send_recv_threads=8, rpc_timeout=120, init_method=f"tcp://{ip}:{port}"
            ),
        )
        print("client"+str(rank)+" is joining")

    rpc.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-rank", type=int, default=1)
    parser.add_argument("-ip", type=str, default="127.0.0.1")
    parser.add_argument("-port", type=int, default=7788)
    parser.add_argument(
        "-dataset", type=str, default="mnist"
    )
    parser.add_argument("-epochs", type=int, default=100)
    parser.add_argument("-world_size", type=int, default=2)
    parser.add_argument('-use_cuda',  type=str, default='True')
    parser.add_argument("-batch_size", type=int, default=500)
    parser.add_argument("-n_critic", type=int, default=1)
    args = parser.parse_args()

    if args.rank is not None:
        # run with a specified rank (need to start up another process with the opposite rank elsewhere)
        run(
            rank=args.rank,
            world_size=args.world_size,
            ip=args.ip,
            port=args.port,
            dataset=args.dataset,
            epochs=args.epochs,
            use_cuda=args.use_cuda,
            batch_size=args.batch_size,
            n_critic=args.n_critic

        )
