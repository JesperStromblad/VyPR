"""
(C) Copyright 2018 CERN and University of Manchester.
This software is distributed under the terms of the GNU General Public Licence version 3 (GPL Version 3), copied verbatim in the file "COPYING".
In applying this licence, CERN does not waive the privileges and immunities granted to it by virtue of its status as an Intergovernmental Organization or submit itself to any jurisdiction.

Author: Joshua Dawes - CERN, University of Manchester - joshua.dawes@cern.ch
"""
"""

This module contains logic for construction of a specialised control flow graph.

"""

import ast

from graphviz import Digraph
from VyPR.monitor_synthesis import formula_tree
from VyPR.QueryBuilding import *

vertices = []


def get_function_name_strings(obj):
    """
    For a given ast object, get the fully qualified function names of all function calls
    found in the object.
    """
    chain = list(ast.walk(obj))
    all_calls = list(filter(lambda item: type(item) is ast.Call, chain))
    ##print(all_calls)
    full_names = {}
    for call in all_calls:
        # construct the full function name for this call
        current_item = call
        full_names[call] = []
        while not (type(current_item) is str):
            if type(current_item) is ast.Call:
                current_item = current_item.func
            elif type(current_item) is ast.Attribute:
                full_names[call].append(current_item.attr)
                current_item = current_item.value
            elif type(current_item) is ast.Name:
                full_names[call].append(current_item.id)
                current_item = current_item.id
            elif type(current_item) is ast.Str:
                # full_names[call].append(current_item.s)
                current_item = current_item.s
            elif type(current_item) is ast.Subscript:
                current_item = current_item.value
    return list(map(lambda item: ".".join(reversed(item)), full_names.values()))


def get_reversed_string_list(obj, omit_subscripts=False):
    """
    For a given ast object, find the reversed list representation of the names inside it.
    Eg, A.b() will give [b, A]
    """
    if type(obj) is ast.Name:
        return [obj.id]
    elif type(obj) is ast.Attribute:
        return [obj.attr] + get_reversed_string_list(obj.value, omit_subscripts=omit_subscripts)
    elif type(obj) is ast.Subscript:
        if omit_subscripts:
            return get_reversed_string_list(obj.value)
        else:
            if type(obj.slice.value) is ast.Str:
                return ["[\"%s\"]" % obj.slice.value.s] + get_reversed_string_list(obj.value,
                                                                                   omit_subscripts=omit_subscripts)
            elif type(obj.slice.value) is ast.Num:
                return ["[%i]" % obj.slice.value.n] + get_reversed_string_list(obj.value,
                                                                               omit_subscripts=omit_subscripts)
            elif type(obj.slice.value) is ast.Name:
                return ["[%s]" % obj.slice.value.id] + get_reversed_string_list(obj.value,
                                                                                omit_subscripts=omit_subscripts)
    elif type(obj) is ast.Call:
        return get_function_name_strings(obj)
    elif type(obj) is ast.Str:
        return [obj.s]


def get_attr_name_string(obj, omit_subscripts=False):
    """
    For an ast object
    """
    attr_string = ""
    if type(obj) in [ast.Load, ast.Index]:
        return None
    else:
        result = get_reversed_string_list(obj, omit_subscripts=omit_subscripts)[::-1]
        for (n, part) in enumerate(result):
            if "." in part and len(result) > 1:
                # all cases in the will be covered individually by traversal
                return None
            else:
                if part[0] != "[":
                    attr_string += "%s%s" % ("." if n != 0 else "", part)
                else:
                    attr_string += part
        return attr_string


class CFGVertex(object):
    """
    This class represents a vertex in a control flow graph.
    Vertices correspond to states - a new state is induced when the value to which
    a name is mapped in Python code is changed.
    """

    def __init__(self, entry=None, path_length=None, structure_obj=None, reference_variables=[]):
        """
        Given the name changed in the state this vertex represents,
        store it.
        Vertices can also have multiple edges leading out of them into next states.
        """
        self._path_length = path_length
        self._structure_obj = structure_obj
        if not (entry):
            self._name_changed = []
        else:
            if type(entry) is ast.Assign and type(entry.value) in [ast.Call, ast.Expr]:
                # only works for a single function being called - should make this recursive
                # for complex expressions that require multiple calls
                if type(entry.targets[0]) is ast.Tuple:
                    self._name_changed = list(map(get_attr_name_string, entry.targets[0].elts) + get_function_name_strings(
                        entry))
                else:
                    self._name_changed = [get_attr_name_string(entry.targets[0])] + get_function_name_strings(entry)
            # TODO: include case where the expression on the right hand side of the assignment is an expression with a call
            elif type(entry) is ast.Expr and type(entry.value) is ast.Call:
                # if there are reference variables, we include them as possibly changed
                self._name_changed = get_function_name_strings(entry.value) + (
                    reference_variables if len(entry.value.args) > 0 else [])
            elif type(entry) is ast.Assign:
                self._name_changed = [get_attr_name_string(entry.targets[0])]
            elif type(entry) is ast.Return:
                if type(entry.value) is ast.Call:
                    self._name_changed = get_function_name_strings(entry.value)
                else:
                    # nothing else could be changed
                    self._name_changed = []
            elif type(entry) is ast.Raise:
                self._name_changed = [entry.type.func.id]
            elif type(entry) is ast.Pass:
                self._name_changed = ["pass"]

        self.edges = []
        self._previous_edge = None

    def add_outgoing_edge(self, edge):
        edge._source_state = self
        self.edges.append(edge)

    def __repr__(self):
        return "<Vertex changing names %s %i>" % (self._name_changed, id(self._name_changed))


class CFGEdge(object):
    """
    This class represents an edge in a control flow graph.
    """

    def __init__(self, condition, instruction=None, input_variables=[]):
        # the condition has to be copied, otherwise later additions to the condition on the same branch
        # for example, to indicate divergence and convergence of control flow
        # will also be reflected in conditions earlier in the branch
        self._condition = [c for c in condition] if type(condition) is list else condition
        self._instruction = instruction
        self._source_state = None
        self._target_state = None
        self._input_variables = input_variables

        if type(self._instruction) is ast.Assign and type(self._instruction.value) in [ast.Call, ast.Expr]:
            # we will have to deal with other kinds of expressions at some point
            if type(self._instruction.targets[0]) is ast.Tuple:
                self._operates_on = list(map(get_attr_name_string,
                                        self._instruction.targets[0].elts) + get_function_name_strings(
                    self._instruction.value))
            else:
                self._operates_on = [get_attr_name_string(self._instruction.targets[0])] + \
                                    get_function_name_strings(self._instruction.value)
        # print(self._operates_on)
        elif type(self._instruction) is ast.Assign and not (type(self._instruction.value) is ast.Call):
            # print("constructed string: ", get_attr_name_string(self._instruction.targets[0]))
            self._operates_on = get_attr_name_string(self._instruction.targets[0])
        elif type(self._instruction) is ast.Expr and hasattr(self._instruction.value, "func"):
            self._operates_on = get_function_name_strings(self._instruction.value)
        elif type(self._instruction) is ast.Return and type(self._instruction.value) is ast.Call:
            self._operates_on = get_function_name_strings(self._instruction.value)
        elif type(self._instruction) is ast.Raise:
            self._operates_on = [self._instruction.type.func.id]
        elif type(self._instruction) is ast.Pass:
            self._operates_on = ["pass"]
        else:
            self._operates_on = [self._instruction]

    def set_target_state(self, state):
        self._target_state = state
        """if not(type(self._instruction) is str):
            state._previous_edge = self"""
        state._previous_edge = self

    def __repr__(self):
        return "<Edge with instruction %s>" % self._instruction


class CFG(object):
    """
    This class represents a symbolic control flow graph.
    """

    def __init__(self, reference_variables=[]):
        self.vertices = []
        self.edges = []
        empty_vertex = CFGVertex()
        self.vertices.append(empty_vertex)
        self.starting_vertices = empty_vertex
        self.return_statements = []
        self.branch_initial_statements = []
        self.reference_variables = reference_variables

    def process_block(self, block, starting_vertices=None, condition=[], input_variables=[]):
        """
        Given a block, a set of starting vertices and to put on the first edge,
        construct the section of the control flow graph corresponding to this block.
        Input variables is a list of variables that reach this block of the code by means other than assignment.
        For example, arguments to a function call (the body of which we construct the SCFG) or loop variables.
        """
        # make a copy of the condition sequence for this branch
        condition = [c for c in condition]
        current_vertices = starting_vertices if not (starting_vertices is None) else [self.starting_vertices]
        # make sure each block that is not nested has independent input variables
        input_variables_copy = [s for s in input_variables]
        path_length = 0

        for (n, entry) in enumerate(block):
            # print("processing block")
            if type(entry) is ast.Assign or (type(entry) is ast.Expr and type(entry.value) is ast.Call):
                path_length += 1
                # print("processing assignment")

                # condition_to_use = condition if n == 0 else []

                # for each vertex in current_vertices, add an edge
                new_edges = []
                for vertex in current_vertices:
                    entry._parent_body = block
                    # print("constructing new edge")
                    new_edge = CFGEdge(condition, entry, input_variables=input_variables_copy)
                    new_edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)

                # create a new vertex for the state created here
                new_vertex = CFGVertex(entry, path_length=path_length, reference_variables=self.reference_variables)

                self.vertices.append(new_vertex)
                self.edges += new_edges

                # direct all new edges to this new vertex
                for edge in new_edges:
                    edge.set_target_state(new_vertex)

                # update current vertices
                current_vertices = [new_vertex]

            if type(entry) is ast.Pass:
                path_length += 1
                entry._parent_body = block

                # for each vertex in current_vertices, add an edge
                new_edges = []
                for vertex in current_vertices:
                    entry._parent_body = block
                    # print("constructing new edge")
                    new_edge = CFGEdge(condition, entry, input_variables=input_variables_copy)
                    new_edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)

                # create a new vertex for the state created here
                new_vertex = CFGVertex(entry, path_length=path_length)

                self.vertices.append(new_vertex)
                self.edges += new_edges

                # direct all new edges to this new vertex
                for edge in new_edges:
                    edge.set_target_state(new_vertex)

                # update current vertices
                current_vertices = [new_vertex]

            elif type(entry) is ast.Return:
                path_length += 1

                # condition_to_use = condition if n == 0 else []

                new_edges = []
                for vertex in current_vertices:
                    entry._parent_body = block
                    new_edge = CFGEdge(condition, entry, input_variables=input_variables_copy)
                    new_edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)

                new_vertex = CFGVertex(entry, path_length=path_length)

                self.vertices.append(new_vertex)
                self.edges += new_edges

                # direct all new edges to this new vertex
                for edge in new_edges:
                    edge.set_target_state(new_vertex)

                # update current vertices
                current_vertices = [new_vertex]

                self.return_statements.append(new_vertex)

            elif type(entry) is ast.Raise:
                path_length += 1

                # condition_to_use = condition if n == 0 else []

                new_edges = []
                for vertex in current_vertices:
                    entry._parent_body = block
                    new_edge = CFGEdge(condition, entry, input_variables=input_variables_copy)
                    new_edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)

                new_vertex = CFGVertex(entry, path_length=path_length)

                self.vertices.append(new_vertex)
                self.edges += new_edges

                # direct all new edges to this new vertex
                for edge in new_edges:
                    edge.set_target_state(new_vertex)

                # update current vertices
                current_vertices = [new_vertex]

            elif type(entry) is ast.If:
                entry._parent_body = block
                # print("Processing conditional at line %i with parent block %s" % (entry.lineno, entry._parent_body))
                path_length += 1

                if entry != entry._parent_body[-1]:
                    self.branch_initial_statements.append(["post-conditional", entry])

                # if we just have an if without an else block, this misses out the edge going from the state
                # before the conditional to the state after it (if the condition is false)
                # compute a list of pairs (condition, block) derived from the conditional
                pairs = [([entry.test], entry.body)]

                current_condition_set = [formula_tree.lnot(entry.test)]

                current_conditional = [entry]
                final_else_is_present = False
                # won't work when there is something after the second if in the else clause
                while type(current_conditional[0]) is ast.If:
                    current_conditional = current_conditional[0].orelse
                    if len(current_conditional) > 0:
                        if type(current_conditional[0]) is ast.If:
                            pairs.append(
                                (current_condition_set + [current_conditional[0].test], current_conditional[0].body))
                            current_condition_set.append(formula_tree.lnot(current_conditional[0].test))
                        else:
                            pairs.append((current_condition_set, current_conditional))
                            final_else_is_present = True
                    else:
                        # nowhere else to go in the traversal
                        break

                # insert intermediate control flow vertex at the beginning of the block
                empty_conditional_vertex = CFGVertex(structure_obj=entry)
                empty_conditional_vertex._name_changed = ['conditional']
                self.vertices.append(empty_conditional_vertex)
                for vertex in current_vertices:
                    new_edge = CFGEdge("conditional", "control-flow")
                    self.edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)
                    new_edge.set_target_state(empty_conditional_vertex)
                current_vertices = [empty_conditional_vertex]

                # add the branches to the graph
                final_conditional_vertices = []
                for (n, pair) in enumerate(pairs):
                    final_vertices = self.process_block(pair[1], current_vertices, pair[0],
                                                        input_variables=input_variables_copy)
                    final_conditional_vertices += final_vertices
                    self.branch_initial_statements.append(["conditional", pair[1][0], n])
                # vertices += final_vertices

                # the disjunction of formulas that could each have been followed for control flow to continue after
                # the if-statement
                disjunction_for_after_branch = current_condition_set  # + map(formula_tree.lnot, current_condition_set)

                # print(final_conditional_vertices)

                # we include the vertex before the conditional, only if there was no else
                if not (final_else_is_present):
                    # we add a branching statement - the branch number is just the number of pairs we found
                    self.branch_initial_statements.append(["conditional-no-else", entry, len(pairs)])
                    current_vertices = final_conditional_vertices + current_vertices
                else:
                    current_vertices = final_conditional_vertices

                # print(current_vertices)

                # filter out vertices that were returns or raises
                # here we have to check for the previous edge existing, in case the program starts with a conditional
                current_vertices = list(filter(
                    lambda vertex: vertex._previous_edge is None or not (
                            type(vertex._previous_edge._instruction) in [ast.Return, ast.Raise]),
                    current_vertices
                ))

                # add an empty "control flow" vertex after the conditional
                # to avoid transition duplication along the edges leaving
                # the conditional
                if len(current_vertices) > 0:
                    empty_vertex = CFGVertex()
                    empty_vertex._name_changed = ['post-conditional']
                    # at the moment, used for grammar construction from the scfg
                    empty_conditional_vertex.post_conditional_vertex = empty_vertex
                    self.vertices.append(empty_vertex)
                    for vertex in current_vertices:
                        # an empty edge
                        new_edge = CFGEdge("post-condition", "control-flow")
                        self.edges.append(new_edge)
                        new_edge.set_target_state(empty_vertex)
                        vertex.add_outgoing_edge(new_edge)

                    current_vertices = [empty_vertex]
                else:
                    empty_conditional_vertex.post_conditional_vertex = None

                # for now, the path length is wrt the start of the current branch, so intermediate branching doesn't matter
                # condition_copy = [c for c in condition]
                # condition_copy.append(disjunction_for_after_branch)
                # condition = condition_copy
                condition.append("skip-conditional")

                # reset path length for instructions after conditional
                path_length = 0

            elif type(entry) is ast.Try:
                entry._parent_body = block
                path_length += 1
                # print("processing try-except")

                if entry != entry._parent_body[-1]:
                    self.branch_initial_statements.append(["post-try-catch", entry])

                # insert intermediate control flow vertex at the beginning of the block
                empty_conditional_vertex = CFGVertex()
                empty_conditional_vertex._name_changed = ['try-catch']
                self.vertices.append(empty_conditional_vertex)
                for vertex in current_vertices:
                    new_edge = CFGEdge("try-catch", "control-flow")
                    self.edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)
                    new_edge.set_target_state(empty_conditional_vertex)
                current_vertices = [empty_conditional_vertex]

                blocks = []
                self.branch_initial_statements.append(["try-catch", entry.body[0], "try-catch-main"])

                # print("except handling blocks are:")

                for except_handler in entry.handlers:
                    self.branch_initial_statements.append(["try-catch", except_handler.body[0], "try-catch-handler"])
                    # print(except_handler.body)
                    blocks.append(except_handler.body)

                # print("final list of except blocks is")
                # print(blocks)

                # print("processing blocks")

                # first process entry.body
                final_try_catch_vertices = []

                final_vertices = self.process_block(entry.body, current_vertices, ['try-catch-main'],
                                                    input_variables=input_variables_copy)
                final_try_catch_vertices += final_vertices

                # now process the except handlers - eventually with some identifier for each branch

                for block_item in blocks:
                    # print(block_item)
                    # print("="*10)
                    final_vertices = self.process_block(block_item, current_vertices, ['try-catch-handler'],
                                                        input_variables=input_variables_copy)
                    final_try_catch_vertices += final_vertices
                # print("="*10)

                current_vertices = final_try_catch_vertices

                # print(current_vertices)

                # filter out vertices that were returns or raises
                # this should be applied to the other cases as well - needs testing
                current_vertices = list(filter(
                    lambda vertex: vertex._previous_edge is None or not (
                            type(vertex._previous_edge._instruction) in [ast.Return, ast.Raise]),
                    current_vertices
                ))

                # print("processing try-except end statements")
                # print(current_vertices)

                if len(current_vertices) > 0:
                    empty_vertex = CFGVertex()
                    empty_vertex._name_changed = ['post-try-catch']
                    empty_conditional_vertex.post_try_catch_vertex = empty_vertex
                    self.vertices.append(empty_vertex)
                    for vertex in current_vertices:
                        # an empty edge
                        new_edge = CFGEdge("post-try-catch", "control-flow")
                        self.edges.append(new_edge)
                        new_edge.set_target_state(empty_vertex)
                        vertex.add_outgoing_edge(new_edge)

                    current_vertices = [empty_vertex]
                else:
                    empty_conditional_vertex.post_try_catch_vertex = None

                condition.append("skip-try-catch")
                path_length = 0

            elif type(entry) is ast.For:
                entry._parent_body = block
                path_length += 1

                # this will eventually be modified to include the loop variable as the state changed

                empty_pre_loop_vertex = CFGVertex()
                empty_pre_loop_vertex._name_changed = ['loop']
                empty_post_loop_vertex = CFGVertex()
                empty_post_loop_vertex._name_changed = ['post-loop']
                self.vertices.append(empty_pre_loop_vertex)
                self.vertices.append(empty_post_loop_vertex)

                # link current_vertices to the pre-loop vertex
                for vertex in current_vertices:
                    new_edge = CFGEdge(entry.iter, "loop")
                    self.edges.append(new_edge)
                    vertex.add_outgoing_edge(new_edge)
                    new_edge.set_target_state(empty_pre_loop_vertex)

                current_vertices = [empty_pre_loop_vertex]

                # process loop body
                # first, determine the additional input variables that this loop induces
                loop_variable = entry.target
                if type(loop_variable) is ast.Name:
                    additional_input_variables = [loop_variable.id]
                elif type(loop_variable) is ast.Tuple:
                    additional_input_variables = list(map(lambda item: item.id, loop_variable.elts))
                final_vertices = self.process_block(entry.body, current_vertices, ['enter-loop'],
                                                    input_variables=input_variables_copy + additional_input_variables)

                # for a for loop, we add a path recording instrument at the beginning of the loop body
                # and after the loop body
                self.branch_initial_statements.append(["loop", entry.body[0], "enter-loop", entry, "end-loop"])

                # add 2 edges from the final_vertex - one going back to the pre-loop vertex
                # with the positive condition, and one going to the post loop vertex.

                for final_vertex in final_vertices:
                    # there will probably only ever be one final vertex, but we register a branching vertex
                    # self.branching_vertices.append(final_vertex)
                    for base_vertex in current_vertices:
                        new_positive_edge = CFGEdge('loop-jump', 'loop-jump')
                        self.edges.append(new_positive_edge)
                        final_vertex.add_outgoing_edge(new_positive_edge)
                        new_positive_edge.set_target_state(base_vertex)

                        new_post_edge = CFGEdge("post-loop", "post-loop")
                        self.edges.append(new_post_edge)
                        final_vertex.add_outgoing_edge(new_post_edge)
                        new_post_edge.set_target_state(empty_post_loop_vertex)

                skip_edge = CFGEdge(formula_tree.lnot(entry.iter), "loop-skip")
                empty_pre_loop_vertex.add_outgoing_edge(skip_edge)
                # skip_edge.set_target_state(final_vertices[0])
                skip_edge.set_target_state(empty_post_loop_vertex)

                current_vertices = [empty_post_loop_vertex]
                # current_vertices = final_vertices

                condition.append("skip-for-loop")

                # reset path length for instructions after loop
                path_length = 0

            elif type(entry) is ast.While:
                # needs work - but while loops haven't been a thing we've needed to handle so far
                # need to add code to deal with branching vertices
                path_length += 1

                # this should be updated at some point to include empty pre and post-loop vertices like in the for loop clause above

                final_vertices = self.process_block(entry.body, current_vertices, ['while'],
                                                    input_variables=input_variables_copy)

                for final_vertex in final_vertices:
                    for base_vertex in current_vertices:
                        new_positive_edge = CFGEdge('for', 'loop-jump')
                        self.edges.append(new_positive_edge)
                        final_vertex.add_outgoing_edge(new_positive_edge)
                        new_positive_edge.set_target_state(base_vertex)

                current_vertices = final_vertices

        return current_vertices

    def derive_grammar(self):
        """
        Derive a dictionary mapping vertices to lists of symbol lists.
        The symbols are either edges (terminal symbols) or vertices (non-terminal symbols).
        """
        # print("constructing context free grammar from scfg")
        final_map = {}
        for vertex in self.vertices:
            # print(vertex)
            # check for the type of vertex
            if len(vertex.edges) == 0:

                # control flow can end at this vertex - the rule for it should just generate the empty string
                final_map[vertex] = [[None]]

            elif not (vertex._name_changed in [["conditional"], ["loop"], ["try-catch"], ["post-conditional"],
                                               ["post-loop"], ["post-try-catch"]]):

                # a normal vertex, but we care about what it leads to since this determines the "special" structure of rules we generate

                # print(vertex._name_changed)
                # we handle conditionals and try-catches together at the moment, because they have similar structure
                if not (vertex.edges[0]._target_state._name_changed in [["conditional"], ["try-catch"]]):

                    # check which vertices this leads to

                    if vertex.edges[0]._target_state._name_changed in [["post-conditional"], ["post-try-catch"]]:
                        final_map[vertex] = [[vertex.edges[0]]]
                    elif any(map(lambda edge: edge._target_state._name_changed == ["post-loop"], vertex.edges)):
                        # we have to deal with some branching
                        reloop_edge = list(filter(lambda edge: edge._target_state._name_changed == ["loop"], vertex.edges))[0]
                        loop_skip_edge = \
                            list(filter(lambda edge: edge._target_state._name_changed != ["loop"], vertex.edges))[0]
                        final_map[vertex] = [[reloop_edge, reloop_edge._target_state], [loop_skip_edge]]
                    elif vertex.edges[0]._target_state._name_changed == ["loop"]:
                        post_loop_vertex = list(filter(
                            lambda edge: edge._target_state._name_changed == ["post-loop"],
                            vertex.edges[0]._target_state.edges
                        ))[0]._target_state
                        final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state, post_loop_vertex]]
                    else:
                        # normal vertex that isn't followed by any special structure
                        if vertex.edges[0]._target_state._name_changed in [["post-conditional"], ["post-loop"],
                                                                           ["post-try-catch"]]:
                            final_map[vertex] = [[vertex.edges[0]]]
                        else:
                            final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state]]

                elif vertex.edges[0]._target_state._name_changed == ["conditional"]:

                    # get the edge that leads to the end of the conditional
                    post_conditional_vertex = vertex.edges[0]._target_state.post_conditional_vertex
                    if post_conditional_vertex:
                        final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state, post_conditional_vertex]]
                    else:
                        final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state]]

                elif vertex.edges[0]._target_state._name_changed == ["try-catch"]:

                    # get the edge that leads to the end of the try-catch
                    post_try_catch_vertex = vertex.edges[0]._target_state.post_try_catch_vertex
                    if post_try_catch_vertex:
                        final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state, post_try_catch_vertex]]
                    else:
                        final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state]]

            elif vertex._name_changed == ["loop"]:

                # find the loop-skip edge
                loop_skip_edge = list(filter(lambda edge: edge._target_state._name_changed == ["post-loop"], vertex.edges))[0]
                final_map[vertex] = [[loop_skip_edge]]
                loop_entry_edge = list(filter(lambda edge: edge._target_state._name_changed != ["post-loop"], vertex.edges))[
                    0]
                final_map[vertex].append([loop_entry_edge, loop_entry_edge._target_state])


            elif vertex._name_changed in [["conditional"], ["try-catch"]]:

                final_map[vertex] = []
                for edge in vertex.edges:
                    # we check whether we're looking at an edge that leads straight past the conditional
                    # and directly to the post-conditional vertex
                    if edge._target_state._name_changed == ["post-conditional"]:
                        final_map[vertex].append([edge])
                    else:
                        final_map[vertex].append([edge, edge._target_state])

            elif vertex._name_changed == ["post-conditional"]:

                # check whether we're inside a loop
                if vertex.edges[0]._target_state._name_changed == ["loop"]:
                    # if we're inside a loop, then we need to include the post-loop edge
                    final_map[vertex] = [
                        [vertex.edges[0], vertex.edges[0]._target_state],
                        [vertex.edges[1]]
                    ]
                elif vertex.edges[0]._target_state._name_changed == ["post-conditional"]:
                    final_map[vertex] = [[vertex.edges[0]]]
                else:
                    final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state]]

            else:

                if vertex.edges[0]._target_state._name_changed in [["post-conditional"], ["post-loop"],
                                                                   ["post-try-catch"]]:
                    final_map[vertex] = [[vertex.edges[0]]]
                else:
                    final_map[vertex] = [[vertex.edges[0], vertex.edges[0]._target_state]]

        # print(final_map[vertex])

        return final_map

    def next_calls(self, vertex, function, calls=[], marked_vertices=[]):
        """
        Given a point (vertex or edge), find the set of next edges that model calls to function.
        """
        if not (vertex in marked_vertices):
            marked_vertices.append(vertex)
            edges = vertex.edges
            for edge in edges:
                if ((type(edge._instruction) is ast.Expr
                     and hasattr(edge._instruction.value, "func")
                     and function in get_function_name_strings(edge._instruction.value))
                        or
                        (type(edge._instruction) is ast.Assign
                         and type(edge._instruction.value) is ast.Call and function in get_function_name_strings(
                                    edge._instruction.value))):
                    calls.append(edge)
                else:
                    # this edge is not what we're looking for
                    # so traverse this branch further
                    self.next_calls(edge._target_state, function, calls, marked_vertices)
        else:
            pass


def expression_as_string(expression):
    if type(expression) is ast.Num:
        return str(expression.n)
    else:
        return str(expression)


def instruction_to_string(instruction):
    if type(instruction) is ast.Assign:
        return "%s = %s" % (get_attr_name_string(instruction.targets[0]), expression_as_string(instruction.value))
    elif type(instruction) is ast.Expr:
        return "%s()" % get_function_name_strings(instruction.value)
