# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for agents.dqn.categorical_dqn_agent."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf
from tf_agents.agents.categorical_dqn import categorical_dqn_agent
from tf_agents.networks import categorical_q_network
from tf_agents.networks import network
from tf_agents.networks import q_rnn_network
from tf_agents.specs import tensor_spec
from tf_agents.trajectories import policy_step
from tf_agents.trajectories import test_utils
from tf_agents.trajectories import time_step as ts
from tf_agents.trajectories import trajectory
from tf_agents.utils import common


class DummyCategoricalNet(network.Network):

  def __init__(self,
               input_tensor_spec,
               num_atoms=51,
               num_actions=2,
               name=None):
    self._num_atoms = num_atoms
    self._num_actions = num_actions
    super(DummyCategoricalNet, self).__init__(
        input_tensor_spec=input_tensor_spec,
        state_spec=(),
        name=name)

    # In CategoricalDQN we are dealing with a distribution over Q-values, which
    # are represented as num_atoms bins, ranging from min_q_value to
    # max_q_value. In order to replicate the setup in the non-categorical
    # network (namely, [[2, 1], [1, 1]]), we use the following "logits":
    # [[0, 1, ..., num_atoms-1, num_atoms, 1, ..., 1],
    #  [1, ......................................, 1]]
    # The important bit is that the first half of the first list (which
    # corresponds to the logits for the first action) place more weight on the
    # higher q_values than on the lower ones, thereby resulting in a higher
    # value for the first action.
    weights_initializer = np.array([
        np.concatenate((np.arange(num_atoms), np.ones(num_atoms))),
        np.concatenate((np.ones(num_atoms), np.ones(num_atoms)))])
    kernel_initializer = tf.compat.v1.initializers.constant(
        weights_initializer, verify_shape=True)
    bias_initializer = tf.compat.v1.initializers.ones()

    # Store custom layers that can be serialized through the Checkpointable API.
    self._dummy_layers = []
    self._dummy_layers.append(
        tf.keras.layers.Dense(
            num_actions * num_atoms,
            kernel_initializer=kernel_initializer,
            bias_initializer=bias_initializer))

  @property
  def num_atoms(self):
    return self._num_atoms

  def call(self, inputs, unused_step_type=None, network_state=()):
    inputs = tf.cast(inputs, tf.float32)
    for layer in self._dummy_layers:
      inputs = layer(inputs)
    logits = tf.reshape(inputs, [-1, self._num_actions, self._num_atoms])
    return logits, network_state


class DummyCategoricalQRnnNetwork(q_rnn_network.QRnnNetwork):

  def __init__(self,
               input_tensor_spec,
               action_spec,
               num_atoms=51,
               **kwargs):
    if not isinstance(action_spec, tensor_spec.BoundedTensorSpec):
      raise TypeError('action_spec must be a BoundedTensorSpec. Got: %s' % (
          action_spec,))

    self._num_actions = action_spec.maximum - action_spec.minimum + 1
    self._num_atoms = num_atoms

    q_network_action_spec = tensor_spec.BoundedTensorSpec(
        (), tf.int32, minimum=0, maximum=self._num_actions * num_atoms - 1)

    super(DummyCategoricalQRnnNetwork, self).__init__(
        input_tensor_spec=input_tensor_spec,
        action_spec=q_network_action_spec,
        **kwargs)

  @property
  def num_atoms(self):
    return self._num_atoms

  def call(self, observations, step_type=None, network_state=None):
    logits, network_state = super(DummyCategoricalQRnnNetwork, self).call(
        observations, step_type, network_state)
    shape = logits.shape.as_list()
    assert shape[-1] == self._num_actions * self._num_atoms
    new_shape = shape[:-1] + [self._num_actions, self._num_atoms]
    logits = tf.reshape(logits, new_shape)
    return logits, network_state


class CategoricalDqnAgentTest(tf.test.TestCase):

  def setUp(self):
    super(CategoricalDqnAgentTest, self).setUp()
    tf.compat.v1.enable_resource_variables()
    self._obs_spec = tensor_spec.TensorSpec([2], tf.float32)
    self._time_step_spec = ts.time_step_spec(self._obs_spec)
    self._action_spec = tensor_spec.BoundedTensorSpec((), tf.int32, 0, 1)
    self._categorical_net = categorical_q_network.CategoricalQNetwork(
        self._obs_spec,
        self._action_spec,
        fc_layer_params=[4])
    self._dummy_categorical_net = DummyCategoricalNet(self._obs_spec)
    self._optimizer = tf.compat.v1.train.GradientDescentOptimizer(0.01)

  def testCreateAgentNestSizeChecks(self):
    action_spec = [
        tensor_spec.BoundedTensorSpec([1], tf.int32, 0, 1),
        tensor_spec.BoundedTensorSpec([1], tf.int32, 0, 1)
    ]

    with self.assertRaisesRegexp(
        ValueError, '.*Only one dimensional actions.*'):
      categorical_dqn_agent.CategoricalDqnAgent(
          self._time_step_spec,
          action_spec,
          self._dummy_categorical_net,
          self._optimizer)

  def testCreateAgentDimChecks(self):
    action_spec = [tensor_spec.BoundedTensorSpec([1, 2], tf.int32, 0, 1)]

    with self.assertRaisesRegexp(
        ValueError, '.*Only one dimensional actions.*'):
      categorical_dqn_agent.CategoricalDqnAgent(
          self._time_step_spec,
          action_spec,
          self._dummy_categorical_net,
          self._optimizer)

  def testCreateAgentDefaultNetwork(self):
    categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._categorical_net,
        self._optimizer)

  def testCriticLoss(self):
    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._dummy_categorical_net,
        self._optimizer)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)

    actions = tf.constant([0, 1], dtype=tf.int32)
    action_steps = policy_step.PolicyStep(actions)

    rewards = tf.constant([10, 20], dtype=tf.float32)
    discounts = tf.constant([0.9, 0.9], dtype=tf.float32)
    next_observations = tf.constant([[5, 6], [7, 8]], dtype=tf.float32)
    next_time_steps = ts.transition(next_observations, rewards, discounts)

    experience = test_utils.stacked_trajectory_from_transition(
        time_steps, action_steps, next_time_steps)

    # Due to the constant initialization of the DummyCategoricalNet, we can
    # expect the same loss every time.
    expected_loss = 2.195
    loss_info = agent._loss(experience)

    self.evaluate(tf.compat.v1.global_variables_initializer())
    evaluated_loss = self.evaluate(loss_info).loss
    self.assertAllClose(evaluated_loss, expected_loss, atol=1e-3)

  def testCriticLossNStep(self):
    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._dummy_categorical_net,
        self._optimizer,
        n_step_update=2)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)

    actions = tf.constant([0, 1], dtype=tf.int32)
    action_steps = policy_step.PolicyStep(actions)

    rewards = tf.constant([10, 20], dtype=tf.float32)
    discounts = tf.constant([0.9, 0.9], dtype=tf.float32)
    next_observations = tf.constant([[5, 6], [7, 8]], dtype=tf.float32)
    next_time_steps = ts.transition(next_observations, rewards, discounts)

    third_observations = tf.constant([[9, 10], [11, 12]], dtype=tf.float32)
    third_time_steps = ts.transition(third_observations, rewards, discounts)

    experience1 = trajectory.from_transition(
        time_steps, action_steps, next_time_steps)
    experience2 = trajectory.from_transition(
        next_time_steps, action_steps, third_time_steps)
    experience3 = trajectory.from_transition(
        third_time_steps, action_steps, third_time_steps)

    experience = tf.nest.map_structure(
        lambda x, y, z: tf.stack([x, y, z], axis=1),
        experience1, experience2, experience3)

    loss_info = agent._loss(experience)

    # discounted_returns should evaluate to 10 + 0.9 * 10 = 19 and
    # 20 + 0.9 * 20 = 38.
    evaluated_discounted_returns = self.evaluate(agent._discounted_returns)
    self.assertAllClose(evaluated_discounted_returns, [[19], [38]], atol=1e-3)

    # Both final_value_discount values should be 0.9 * 0.9 = 0.81.
    evaluated_final_value_discount = self.evaluate(agent._final_value_discount)
    self.assertAllClose(evaluated_final_value_discount, [[0.81], [0.81]],
                        atol=1e-3)

    # Due to the constant initialization of the DummyCategoricalNet, we can
    # expect the same loss every time.
    expected_loss = 2.195
    self.evaluate(tf.compat.v1.global_variables_initializer())
    evaluated_loss = self.evaluate(loss_info).loss
    self.assertAllClose(evaluated_loss, expected_loss, atol=1e-3)

  def testPolicy(self):
    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._categorical_net,
        self._optimizer)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions, _, _ = agent.policy.action(time_steps)
    self.assertEqual(actions.shape, [2])
    self.evaluate(tf.compat.v1.global_variables_initializer())
    actions_ = self.evaluate(actions)
    self.assertTrue(all(actions_ <= self._action_spec.maximum))
    self.assertTrue(all(actions_ >= self._action_spec.minimum))

  def testInitialize(self):
    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._categorical_net,
        self._optimizer)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([0, 1], dtype=tf.int32)
    action_steps = policy_step.PolicyStep(actions)

    rewards = tf.constant([10, 20], dtype=tf.float32)
    discounts = tf.constant([0.9, 0.9], dtype=tf.float32)
    next_time_steps = ts.transition(observations, rewards, discounts)

    experience = test_utils.stacked_trajectory_from_transition(
        time_steps, action_steps, next_time_steps)

    loss_info = agent._loss(experience)
    initialize = agent.initialize()

    self.evaluate(tf.compat.v1.global_variables_initializer())
    losses = self.evaluate(loss_info).loss
    self.assertGreater(losses, 0.0)

    critic_variables = agent._q_network.variables
    target_critic_variables = agent._target_q_network.variables
    self.assertTrue(critic_variables)
    self.assertTrue(target_critic_variables)
    self.evaluate(initialize)
    for s, t in zip(critic_variables, target_critic_variables):
      self.assertAllClose(self.evaluate(s), self.evaluate(t))

  def testUpdateTarget(self):
    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._categorical_net,
        self._optimizer)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([0, 1], dtype=tf.int32)
    action_steps = policy_step.PolicyStep(actions)
    experience = test_utils.stacked_trajectory_from_transition(
        time_steps, action_steps, time_steps)

    loss_info = agent._loss(experience)
    update_targets = agent._update_target()

    self.evaluate(tf.compat.v1.global_variables_initializer())
    losses = self.evaluate(loss_info).loss
    self.assertGreater(losses, 0.0)
    self.evaluate(update_targets)

  def testTrain(self):
    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        self._action_spec,
        self._dummy_categorical_net,
        self._optimizer)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)

    actions = tf.constant([0, 1], dtype=tf.int32)
    action_steps = policy_step.PolicyStep(actions)

    rewards = tf.constant([10, 20], dtype=tf.float32)
    discounts = tf.constant([0.9, 0.9], dtype=tf.float32)
    next_observations = tf.constant([[5, 6], [7, 8]], dtype=tf.float32)
    next_time_steps = ts.transition(next_observations, rewards, discounts)

    experience = test_utils.stacked_trajectory_from_transition(
        time_steps, action_steps, next_time_steps)

    train_step = agent.train(experience, weights=None)

    # Due to the constant initialization of the DummyCategoricalNet, we can
    # expect the same loss every time.
    expected_loss = 2.195
    self.evaluate(tf.compat.v1.global_variables_initializer())
    evaluated_loss, _ = self.evaluate(train_step)
    self.assertAllClose(evaluated_loss, expected_loss, atol=1e-3)

  def testTrainWithRnn(self):
    action_spec = tensor_spec.BoundedTensorSpec([1], tf.int32, 0, 1)

    batch_size = 5
    observations = tf.constant(
        [[[1, 2], [3, 4], [5, 6]]] * batch_size, dtype=tf.float32)
    actions = tf.constant([[[0], [1], [1]]] * batch_size, dtype=tf.int32)
    time_steps = ts.TimeStep(
        step_type=tf.constant([[1] * 3] * batch_size, dtype=tf.int32),
        reward=tf.constant([[1] * 3] * batch_size, dtype=tf.float32),
        discount=tf.constant([[1] * 3] * batch_size, dtype=tf.float32),
        observation=[observations])

    experience = trajectory.Trajectory(
        step_type=time_steps.step_type,
        observation=observations,
        action=actions,
        policy_info=(),
        next_step_type=time_steps.step_type,
        reward=time_steps.reward,
        discount=time_steps.discount)

    categorical_q_rnn_network = DummyCategoricalQRnnNetwork(
        self._obs_spec,
        action_spec,
        conv_layer_params=None,
        input_fc_layer_params=(16,),
        preprocessing_combiner=None,
        lstm_size=(40,),
        output_fc_layer_params=(16,),
    )

    counter = common.create_variable('test_train_counter')

    agent = categorical_dqn_agent.CategoricalDqnAgent(
        self._time_step_spec,
        action_spec,
        categorical_q_rnn_network,
        optimizer=tf.compat.v1.train.AdamOptimizer(0.001),
    )

    # Force variable creation.
    agent.policy.variables()
    if tf.executing_eagerly():
      loss = lambda: agent.train(experience)
    else:
      loss = agent.train(experience)

    self.evaluate(tf.compat.v1.initialize_all_variables())
    self.assertEqual(self.evaluate(counter), 0)
    self.evaluate(loss)


if __name__ == '__main__':
  tf.test.main()
