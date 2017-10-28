#!/usr/bin/env python
# Copyright (c) 2017 Trail of Bits, Inc.
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

from __future__ import absolute_import
import angr
import collections
import logging
import sys

L = logging.getLogger("mctest")
L.setLevel(logging.INFO)

def hook_function(project, ea, cls):
  """Hook the function `name` with the SimProcedure `cls`."""
  project.hook(ea, cls(project=project))


def read_c_string(state, ea):
  """Read a concrete NUL-terminated string from `ea`."""
  assert isinstance(ea, (int, long))
  chars = []
  i = 0
  while True:
    char = state.mem[ea + i].char.resolved
    char = state.solver.eval(char, cast_to=str)
    if not ord(char[0]):
      break
    chars.append(char)
    i += 1
  return "".join(chars)


def read_uintptr_t(state, ea):
  """Read a uint64_t value from memory."""
  next_ea = ea + (state.arch.bits // 8)
  val = state.solver.eval(state.mem[ea].uintptr_t.resolved, cast_to=int)
  return val, next_ea


def read_uint32_t(state, ea):
  """Read a uint64_t value from memory."""
  next_ea = ea + (state.arch.bits // 8)
  val = state.solver.eval(state.mem[ea].uint32_t.resolved, cast_to=int)
  return val, next_ea


TestInfo = collections.namedtuple(
    'TestInfo', 'ea name file_name line_number')


def read_test_info(state, ea):
  """Read in a `McTest_TestInfo` info structure from memory."""
  prev_test_ea, ea = read_uintptr_t(state, ea)
  test_func_ea, ea = read_uintptr_t(state, ea)
  test_name_ea, ea = read_uintptr_t(state, ea)
  file_name_ea, ea = read_uintptr_t(state, ea)
  file_line_num, _ = read_uint32_t(state, ea)

  if not test_func_ea or \
     not test_name_ea or \
     not file_name_ea or \
     not file_line_num:  # `__LINE__` in C always starts at `1` ;-)
    return None, prev_test_ea

  test_name = read_c_string(state, test_name_ea)
  file_name = read_c_string(state, file_name_ea)
  info = TestInfo(test_func_ea, test_name, file_name, file_line_num)
  return info, prev_test_ea


def read_api_table(state, ea):
  """Reads in the API table."""
  apis = {}
  while True:
    api_name_ea, ea = read_uintptr_t(state, ea)
    api_ea, ea = read_uintptr_t(state, ea)
    if not api_name_ea or not api_ea:
      break
    api_name = read_c_string(state, api_name_ea)
    apis[api_name] = api_ea
  return apis


def find_test_cases(state, info_ea):
  """Find the test case descriptors."""
  tests = []
  while info_ea:
    test, info_ea = read_test_info(state, info_ea)
    if test:
      tests.append(test)
  tests.sort(key=lambda t: (t.file_name, t.line_number))
  return tests


def make_symbolic_input(state, input_begin_ea, input_end_ea):
  """Fill in the input data array with symbolic data."""
  input_size = input_end_ea - input_begin_ea
  data = state.se.Unconstrained('MCTEST_INPUT', input_size * 8)
  state.memory.store(input_begin_ea, data)
  return data


class IsSymbolicUInt(angr.SimProcedure):
  """Implements McTest_IsSymblicUInt, which returns 1 if its input argument
  has more then one solutions, and zero otherwise."""
  def run(self, arg):
    solutions = self.state.solver.eval_upto(arg, 2)
    if not solutions:
      return 0
    elif 1 == len(solutions):
      if self.state.se.symbolic(arg):
        self.state.solver.add(arg == solutions[0])
      return 0
    else:
      return 1


class Assume(angr.SimProcedure):
  """Implements _McTest_CanAssume, which tries to inject a constraint."""
  def run(self, arg):
    constraint = arg != 0
    self.state.solver.add(constraint)
    if not self.state.solver.satisfiable():
      L.error("Failed to assert assumption {}".format(constraint))
      self.exit(2)


class Pass(angr.SimProcedure):
  """Implements McTest_Pass, which notifies us of a passing test."""
  def run(self):
    L.info("Passed test case")
    self.exit(0)


class Fail(angr.SimProcedure):
  """Implements McTest_Fail, which notifies us of a passing test."""
  def run(self):
    L.error("Failed test case")
    self.exit(1)


def main():
  """Run McTest."""
  if 2 > len(sys.argv):
    return 1

  project = angr.Project(
      sys.argv[1],
      use_sim_procedures=True,
      translation_cache=True,
      support_selfmodifying_code=False,
      auto_load_libs=False)

  entry_state = project.factory.entry_state()
  addr_size_bits = entry_state.arch.bits

  # Concretely execute up until `McTest_InjectAngr`.
  concrete_manager = angr.SimulationManager(
        project=project,
        active_states=[entry_state])
  run_ea = project.kb.labels.lookup('McTest_Run')
  concrete_manager.explore(find=run_ea)
  run_state = concrete_manager.found[0]
    
  # Read the API table, which will tell us about the location of various
  # symbols. Technically we can look these up with the `labels.lookup` API,
  # but we have the API table for Manticore-compatibility, so we may as well
  # use it. 
  ea_of_api_table = project.kb.labels.lookup('McTest_API')
  apis = read_api_table(run_state, ea_of_api_table)

  # Introduce symbolic input that the tested code will use.
  symbolic_input = make_symbolic_input(
      run_state, apis['InputBegin'], apis['InputEnd'])

  # Hook various functions.
  hook_function(project, apis['IsSymbolicUInt'], IsSymbolicUInt)
  hook_function(project, apis['Assume'], Assume)
  hook_function(project, apis['Pass'], Pass)
  hook_function(project, apis['Fail'], Fail)

  # Find the test cases that we want to run.
  tests = find_test_cases(run_state, apis['LastTestInfo'])

  # For each test, create a simulation manager whose initial state calls into
  # the test case function.
  test_managers = []
  for test in tests:
    test_state = project.factory.call_state(
        test.ea,
        base_state=run_state)

    test_manager = angr.SimulationManager(
        project=project,
        active_states=[test_state])

    L.info("Running test case {} from {}:{}".format(
        test.name, test.file_name, test.line_number))
    test_manager.run()

    for state in test_manager.deadended:
      last_event = state.history.events[-1]
      if 'terminate' == last_event.type:
        code = last_event.objects['exit_code']._model_concrete.value
    
  return 0

if "__main__" == __name__:
  exit(main())
