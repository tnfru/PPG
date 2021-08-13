import wandb
import torch as T
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.distributions.categorical import Categorical

from networks import PPG, CriticNet
from utils import approx_kl_div
from trajectory import Trajectory
from utils import clipped_value_loss


class Agent:
    def __init__(self, env, action_dim, state_dim, config):
        self.env = env
        self.actor = PPG(action_dim, state_dim)
        self.critic = CriticNet(state_dim)
        self.discount_factor = 0.99
        self.batch_size = config['batch_size']
        self.clip_ratio = config['clip_ratio']
        self.actor_opt = optim.Adam(self.actor.parameters(),
                                    lr=config['actor_lr'])
        self.critic_opt = optim.Adam(self.critic.parameters(),
                                     lr=config['critic_lr'])
        self.critic_loss_fun = nn.MSELoss()
        self.device = T.device(
            'cuda:0' if T.cuda.is_available() else 'cpu')
        self.train_iterations = config['train_iterations']
        self.aux_iterations = config['aux_iterations']
        self.rollout_length = config['rollout_length']
        self.entropy_coeff = config['entropy_coeff']
        self.kl_max = config['kl_max']
        self.val_coeff = config['val_coeff']
        self.grad_norm = config['grad_norm']
        self.trajectory = Trajectory()
        self.beta = config['beta']
        self.use_wandb = config['use_wandb']
        self.value_clip = config['value_clip']
        self.entropy_decay = config['entropy_decay']
        self.aux_freq = 32
        self.steps = 0

        if self.use_wandb:
            # initialize logging
            wandb.init(project="cartpole", config=config)
            wandb.watch(self.actor, log="all")
            wandb.watch(self.critic, log="all")
            wandb.run.name = 'PPG_' + wandb.run.name

    def get_action(self, state):
        with T.no_grad():
            action_probs, aux_value = self.actor(state)

            action_dist = Categorical(logits=action_probs)
            action = action_dist.sample()
            log_prob = action_dist.log_prob(action).item()
            log_dist = action_dist.probs.log().cpu().detach()

            return action.item(), log_prob, aux_value.item(), log_dist

    def train_ppo_epoch(self, loader):
        for states, actions, expected_returns, log_probs, advantages in \
                loader:
            states = states.to(self.device)
            actions = actions.to(self.device)
            expected_returns = expected_returns.to(self.device).unsqueeze(1)
            advantages = advantages.to(self.device)
            log_probs = log_probs.to(self.device)

            self.train_policy_net(states, actions, log_probs, advantages)
            self.train_critic(states, expected_returns)

    def train_policy_net(self, states, actions, log_probs, advantages):
        action_probs, _ = self.actor(states)
        action_dist = Categorical(logits=action_probs)
        new_log_probs = action_dist.log_prob(actions)
        # log trick for efficient computational graph during backprop
        ratio = T.exp(new_log_probs - log_probs)

        kl_div = approx_kl_div(new_log_probs, log_probs, ratio)

        if kl_div > self.kl_max:
            # If KL divergence is too big we don't take gradient steps
            if self.use_wandb:
                wandb.log({'kl_exceeded': 1})
            return

        entropy_loss = action_dist.entropy().mean() * self.entropy_coeff
        # add entropy for exploration

        weighted_objective = advantages * ratio
        clipped_objective = T.clamp(ratio, 1 - self.clip_ratio,
                                    1 + self.clip_ratio) * advantages
        objective = -T.min(weighted_objective,
                           clipped_objective).mean() - entropy_loss

        self.actor_opt.zero_grad()
        nn.utils.clip_grad_norm_(self.actor.parameters(),
                                 self.grad_norm)
        objective.backward(retain_graph=True)
        self.actor_opt.step()

        if self.use_wandb:
            wandb.log({'entropy': entropy_loss,
                       'kl div': kl_div.mean(),
                       'kl_exceeded': 0})

    def learn(self):
        for epoch in range(self.train_iterations):
            loader = DataLoader(self.trajectory, batch_size=self.batch_size,
                                shuffle=True, pin_memory=True)
            self.train_ppo_epoch(loader)
            self.entropy_coeff *= self.entropy_decay
            self.steps += 1

        if self.steps == self.aux_freq:
            self.steps = 0
            for aux_epoch in range(self.aux_iterations):
                loader = DataLoader(self.trajectory, batch_size=self.batch_size,
                                    shuffle=True, pin_memory=True)
                self.train_aux_epoch(loader)

    def train_aux_epoch(self, loader):
        self.trajectory.is_aux_epoch = True

        for states, expected_returns, log_dists, aux_vals in loader:
            expected_returns = expected_returns.to(self.device).unsqueeze(1)
            states = states.to(self.device)

            self.train_aux_net(states, expected_returns, log_dists, aux_vals)
            self.train_critic(states, expected_returns)

        self.trajectory.is_aux_epoch = False

    def train_aux_net(self, states, expected_returns, old_log_probs,
                      old_aux_value):

        action_probs, aux_values = self.actor(states)
        action_dist = Categorical(logits=action_probs)
        kl_div = approx_kl_div(action_dist.probs.log(), old_log_probs)

        if kl_div > self.kl_max:
            # If KL divergence is too big we don't take gradient steps
            if self.use_wandb:
                wandb.log({'kl_exceeded': 1})
            return

        aux_value_loss = clipped_value_loss(aux_values,
                                            expected_returns,
                                            old_aux_value,
                                            self.value_clip)

        aux_loss = self.val_coeff * aux_value_loss + kl_div * self.beta
        self.actor_opt.zero_grad()
        nn.utils.clip_grad_norm_(self.actor.parameters(),
                                 self.grad_norm)
        aux_loss.backward()
        self.actor_opt.step()

        if self.use_wandb:
            wandb.log({'aux state value': aux_values.mean(),
                       'aux loss': aux_loss.mean(),
                       'aux kl_div': kl_div})

    def train_critic(self, states, expected_returns):
        state_values = self.critic(states)
        critic_loss = self.critic_loss_fun(state_values, expected_returns)
        # critic_loss = clipped_value_loss()

        self.critic_opt.zero_grad()
        nn.utils.clip_grad_norm_(self.critic.parameters(),
                                 self.grad_norm)
        critic_loss.backward()
        self.critic_opt.step()

        if self.use_wandb:
            wandb.log({'critic loss': critic_loss.mean(),
                       'critic state value': state_values.mean()})

    def forget(self):
        self.trajectory = Trajectory()