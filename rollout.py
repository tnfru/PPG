import torch as T
from einops import rearrange

from ppg.trajectory import Trajectory
from pretrain.reward import calc_pretrain_rewards
from pretrain.state_data import StateData
from utils.logger import log_episode_length, log_particle_reward, \
    log_rewards, log_steps_done, log_ppo_env_steps
from utils.logger import log_running_estimates


def run_episode(agent: T.nn.Module, trajectory: Trajectory, pretrain: bool,
                total_steps_done: int):
    """
    Runs one episode in the set environment
    Args:
        agent: agent handling the episode
        trajectory: place to store data in
        total_steps_done: current timestep
        pretrain: if reward is given by environment or calculated in a self
        supervised fashion

    Returns: Trajectory containing the episode data

    """
    state = agent.env.reset()
    rewards = []
    steps_before = len(trajectory)
    done = False
    lives = agent.env.unwrapped.ale.lives()

    while not (lives == 0 and done):
        state = T.tensor(state, dtype=T.float, device=agent.device)
        state = rearrange(state, 'h w c -> 1 c h w')
        action, log_prob, aux_val, log_dist = agent.get_action(state)
        state_val = agent.critic(state).squeeze().item()
        state = state.cpu()
        next_state, reward, done, _ = agent.env.step(action)
        rewards.append(reward)

        trajectory.append_step(state, state_val, action, done,
                               log_prob, aux_val, log_dist)

        state = next_state
        lives = agent.env.unwrapped.ale.lives()

    episode_length = len(rewards)

    if pretrain:
        state_dset = StateData(trajectory.states[steps_before:])
        state_dset.fix_datatypes()
        particle_rewards = calc_pretrain_rewards(agent, state_dset).tolist()
        trajectory.append_rewards(particle_rewards)

        if agent.use_wandb:
            log_particle_reward(agent, particle_rewards,
                                agent.reward_function.mean)
            log_running_estimates(agent, agent.reward_function.mean,
                                  agent.reward_function.var)

    else:
        trajectory.append_rewards(rewards)

    trajectory.calc_advantages(agent.config)

    if agent.use_wandb:
        log_rewards(agent, rewards)
        log_episode_length(agent, episode_length)
        log_steps_done(agent, total_steps_done)
        agent.log_metrics()

    return trajectory, total_steps_done + episode_length


def run_timesteps(agent: T.nn.Module, num_timesteps: int, is_pretrain: bool):
    """
    Runs given number of timesteps
    Args:
        agent: agent handling the interactions
        num_timesteps: steps to train for
        is_pretrain: if reward is given by environment or calculated in a self
        supervised fashion
    """
    steps_done = 0
    agent.forget()

    while steps_done < num_timesteps:
        agent.trajectory, steps_done = run_episode(agent, agent.trajectory,
                                                   is_pretrain,
                                                   steps_done)

        if len(agent.trajectory) >= agent.config['rollout_length']:
            agent.trajectory.data_to_tensors()
            if agent.use_wandb:
                log_ppo_env_steps(agent, steps_done)

            agent.learn(is_pretrain=is_pretrain)

            agent.forget()
            agent.save_model()
