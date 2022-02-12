import torch as T

from utils.logger import log_particle_reward, log_running_estimates


class ParticleReward(T.nn.Module):
    def __init__(self, top_k=5):
        super().__init__()
        self.mean = T.tensor(0.0)
        self.var = T.tensor(1.0)
        self.samples_done = T.tensor(0)
        self.c = T.tensor(1)
        self.top_k = T.tensor(top_k)

    def forward(self, states, normalize=True):
        return self.calculate_reward(states, normalize)

    @T.no_grad()
    def calculate_reward(self, states: T.tensor, normalize=True):
        """
        to calculate apt reward, we approximate a hypersphere around each
        particle (single column entry in latent space). the reward is
        roughly equal to the volume of the hypersphere in comparison to its
        kNN

        Args:
            states: states to calculate reward for
            normalize: if rewards should be normalized

        Returns: particle rewards, T.tensor

        """

        particle_volumes = T.norm(states.unsqueeze(1) - states.unsqueeze(0),
                                  dim=-1)
        if self.top_k > len(particle_volumes):
            # If the size of the last batch is smaller than the number of kNN
            top_k = len(particle_volumes)
            top_k_rewards, _ = particle_volumes.topk(top_k, sorted=True,
                                                     largest=False, dim=1)
        else:
            top_k_rewards, _ = particle_volumes.topk(self.top_k, sorted=True,
                                                     largest=False, dim=1)

        if normalize:
            self.update_estimates(top_k_rewards.reshape(-1, 1))
            top_k_rewards /= self.mean

        top_k_rewards = top_k_rewards.mean(dim=1)

        if not T.isfinite(top_k_rewards).all():
            print('kNN is NaN')
            top_k_rewards[top_k_rewards.isnan()] = 0  # Vectorized Stability

        particle_rewards = T.log(self.c + top_k_rewards)

        return particle_rewards

    def update_estimates(self, x):
        """ Updates running estimates of mean and var"""
        batch_size = x.size(0)
        difference = x.mean(dim=0) - self.mean
        total_samples_done = self.samples_done + batch_size
        batch_var = x.var(dim=0)

        self.update_mean_estimate(difference, batch_size, total_samples_done)
        self.update_var_estimate(difference, batch_var, batch_size,
                                 total_samples_done)

        self.samples_done = total_samples_done

    def update_var_estimate(self, difference, batch_var, batch_size,
                            total_samples_done):
        # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Parallel_algorithm
        var_so_far = self.var * self.samples_done
        var_batch = batch_var * batch_size

        scaled_difference = T.square(
            difference) * batch_size * self.samples_done / total_samples_done

        combined_vars = var_so_far + var_batch + scaled_difference
        self.var = combined_vars / total_samples_done

    def update_mean_estimate(self, difference, batch_size, total_samples_done):
        self.mean = self.mean + difference * batch_size / total_samples_done


@T.no_grad()
def calc_pretrain_rewards(agent: T.nn.Module):
    """

    Args:
        agent: agent whose reward function will be used

    Returns: rewards for all given states

    """

    state_set = agent.trajectory.next_states.to(agent.device)
    representations = agent.contrast_net(state_set)
    particle_rewards = agent.reward_function.calculate_reward(representations)

    agent.trajectory.rewards = particle_rewards.cpu()

    log_particle_reward(agent, particle_rewards)
    log_running_estimates(agent)
