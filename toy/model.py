from torch.autograd import grad
import torch
import torch.nn as nn
from utils import *
from mmd import *
from networks import *
from torch import optim
from tqdm import tqdm
from torch.distributions.multivariate_normal import MultivariateNormal
from torch.autograd.functional import jacobian
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

def is_psd(Q):
    e, _ = torch.linalg.eig(Q)
    if not torch.all(e.real > 1e-7):
        return False
    else:
        return True


def gibbs_sampler(x_init, backward_sampler, opt):
    x=x_init.to(opt['device'])
    samples=[]
    for i in tqdm(range(opt['gibbs_steps'])):
        noisy_x=x+torch.randn_like(x)*opt['noise_std']
        x=backward_sampler(noisy_x)
        samples.append(x.cpu())
    return torch.cat(samples, dim=0)

class DenoiserLearnedVar(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.x_dim=opt['x_dim']
        self.device=opt['device']
        self.net=FeedFowardNet(input_dim=self.x_dim, output_dim=self.x_dim*2,h_layer_num=opt['layer_num'],act=opt['act']).to(opt['device'])
        self.optimizer=optim.Adam(self.net.parameters(), lr=opt['lr'])

    def forward(self, noisy_x):
        mu,log_sigma=self.net(noisy_x).chunk(2,-1)
        sigma=torch.exp(log_sigma)
        return mu,sigma

    def logp_x_tx(self, x,noisy_x):
        noisy_x=noisy_x.to(self.device)
        x=x.to(self.device)
        mu,sigma=self.forward(noisy_x)
        return Normal(mu,sigma).log_prob(x).sum(1)
    
    def sample(self, noisy_x):
        with torch.no_grad():
            mu,sigma=self.forward(noisy_x)
        return Normal(mu,sigma).sample()
    





class DenoisingEBM(nn.Module):
    """Energy paramaterisation.

    We choose to have output shape same as input shape - why not just a scalar?
    """

    def __init__(self, opt):
        super().__init__()
        self.x_dim=opt['x_dim']
        self.device=opt['device']
        self.noise_std=opt['noise_std']
        self.net=FeedFowardNet(input_dim=self.x_dim, output_dim=self.x_dim,h_layer_num=opt['layer_num'],act=opt['act']).to(opt['device'])
        self.optimizer=optim.Adam(self.net.parameters(), lr=opt['lr'])
        self.iso_cov=None

    def forward(self, noisy_x):
        noisy_x=noisy_x.requires_grad_()
        energy=self.net(noisy_x).sum()
        x_score=-grad(energy,noisy_x,create_graph=True)[0]
        denoised_x_mean=noisy_x+self.noise_std**2*x_score
        return denoised_x_mean

    def isotropic_cov_estimation(self, dataset):
        tx_dataset=(dataset+torch.randn_like(dataset)*self.noise_std).requires_grad_()
        energy_tx_dataset=self.net(tx_dataset.to(self.device)).sum()
        x_score_dataset=-grad(energy_tx_dataset,tx_dataset,retain_graph=True,create_graph=True)[0]
        with torch.no_grad():
            self.iso_cov=self.noise_std**2-self.noise_std**4*((x_score_dataset**2).sum(1).mean()/2)
        return self.iso_cov

    def dist_p_x_tx_isotropic_cov(self, noisy_x):
        noisy_x=noisy_x.to(self.device)
        if self.iso_cov is None:
            return "please estimate isotropic covariance first"
        x_mu=self.forward(noisy_x)

        assert x_mu.ndim == 2
        # Batched construction of diagonal covariance matrix
        # (TODO: maybe theres a simpler route using Independent.)
        batch_size, n = x_mu.shape
        diagonal_matrices = torch.zeros(batch_size, n, n, device=x_mu.device, dtype=x_mu.dtype)
        indices = torch.arange(n, device=x_mu.device)
        diagonal_matrices[:, indices, indices] = self.iso_cov
        # log_prob=MultivariateNormal(x_mu,torch.diag(torch.ones_like(x_mu)*self.iso_cov)).log_prob(x)
        return MultivariateNormal(x_mu, diagonal_matrices)

    def logp_x_tx_isotropic_cov(self, x,noisy_x):
        """Modified to work with batched inputs.

        The problem originally was the use of diag to build a covariance matrix:
            diag doesn't operate on batched tensors.
        """
        x=x.to(self.device)
        dist = self.dist_p_x_tx_isotropic_cov(noisy_x)
        return dist.log_prob(x).detach()

    # Prob doesn't work batched
    def sample_isotropic_cov(self, noisy_x):
        if self.iso_cov is None:
            return "please estimate isotropic covariance first"
        x_mu=self.forward(noisy_x)
        return MultivariateNormal(x_mu,torch.diag(torch.ones_like(x_mu[0])*self.iso_cov)).sample()

    def get_hessian(self,noisy_x):
        def get_score_sum(noisy_x):
            energy=self.net(noisy_x).sum()
            score= grad(energy,noisy_x,retain_graph=True,create_graph=True)
            return -score[0].sum(0)
        return  jacobian(lambda a:  get_score_sum(a), noisy_x, vectorize=True).swapaxes(0, 1)

    def dist_p_x_tx_full_cov(self, noisy_x):
        assert noisy_x.ndim == 2
        noisy_x=noisy_x.to(self.device).requires_grad_()
        x_mu=self.forward(noisy_x)

        batch_size, n = x_mu.shape
        diagonal_matrices = torch.zeros(batch_size, n, n, device=x_mu.device, dtype=x_mu.dtype)
        indices = torch.arange(n, device=x_mu.device)
        diagonal_matrices[:, indices, indices] = self.noise_std**2

        hessian_matrix = self.get_hessian(noisy_x)
        with torch.no_grad():
            x_cov=self.noise_std**4*hessian_matrix+diagonal_matrices
            return MultivariateNormal(x_mu,x_cov)

    def logp_x_tx_full_cov(self, x,noisy_x):
        """Modified to work with batched inputs.

        The problem originally was the use of diag to build a covariance matrix.
        """
        x=x.to(self.device)
        dist = self.dist_p_x_tx_full_cov(noisy_x)
        return dist.log_prob(x).detach()

    # Prob doesn't work batched
    def sample_full_cov(self, noisy_x):
        noisy_x=noisy_x.view(1,2).requires_grad_()
        x_mu=self.forward(noisy_x)
        hessian_matrix = self.get_hessian(noisy_x)
        with torch.no_grad():
            x_cov=self.noise_std**4*hessian_matrix+torch.diag(torch.ones(2).to(self.device))*(self.noise_std)**2
            if is_psd(x_cov[0])==False:
                x_cov = x_cov.view(2,2) + torch.diag(torch.ones(2, device=self.device)) * 1e-2
            return MultivariateNormal(x_mu, x_cov).sample()
