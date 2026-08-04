// Dependency graph over tasks: validation, topological order, level grouping.
#ifndef TASKFLOW_GRAPH_H
#define TASKFLOW_GRAPH_H

#include <map>
#include <vector>

#include "task.h"

namespace taskflow {

// Adjacency built from Task::deps, keyed by task id.
class DependencyGraph {
public:
    DependencyGraph();

    void add_task(const Task &task);
    bool has_task(int id) const;
    size_t size() const;

    const std::vector<int> &dependencies_of(int id) const;
    std::vector<int> dependents_of(int id) const;

    // Number of unmet dependencies for `id` given the already-finished set.
    int unmet_count(int id, const std::vector<int> &finished) const;

    // Ids with no dependencies inside the graph.
    std::vector<int> roots() const;

    // Leaves-first order. Returns false (and an empty order) on a cycle.
    bool topological_order(std::vector<int> &out) const;

    // Groups ids so that everything in level N depends only on levels < N.
    std::vector<std::vector<int> > levels() const;

    // Longest dependency chain length, 0 for an empty graph.
    int critical_depth() const;

private:
    std::map<int, std::vector<int> > edges_;
    std::vector<int> order_added_;

    bool visit(int id, std::map<int, int> &mark, std::vector<int> &out) const;
};

// True when every dependency named by a task exists in `tasks`.
bool dependencies_resolvable(const std::vector<Task> &tasks);

DependencyGraph build_graph(const std::vector<Task> &tasks);

}  // namespace taskflow

#endif
