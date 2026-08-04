#include "graph.h"

#include <algorithm>

namespace taskflow {

static const std::vector<int> kNoDeps;

DependencyGraph::DependencyGraph() : edges_(), order_added_() {}

void DependencyGraph::add_task(const Task &task) {
    if (edges_.find(task.id) == edges_.end()) {
        order_added_.push_back(task.id);
    }
    edges_[task.id] = task.deps;
}

bool DependencyGraph::has_task(int id) const {
    return edges_.find(id) != edges_.end();
}

size_t DependencyGraph::size() const {
    return edges_.size();
}

const std::vector<int> &DependencyGraph::dependencies_of(int id) const {
    std::map<int, std::vector<int> >::const_iterator it = edges_.find(id);
    if (it == edges_.end()) {
        return kNoDeps;
    }
    return it->second;
}

std::vector<int> DependencyGraph::dependents_of(int id) const {
    std::vector<int> out;
    std::map<int, std::vector<int> >::const_iterator it;
    for (it = edges_.begin(); it != edges_.end(); ++it) {
        const std::vector<int> &deps = it->second;
        if (std::find(deps.begin(), deps.end(), id) != deps.end()) {
            out.push_back(it->first);
        }
    }
    return out;
}

int DependencyGraph::unmet_count(int id, const std::vector<int> &finished) const {
    const std::vector<int> &deps = dependencies_of(id);
    int missing = 0;
    for (size_t i = 0; i < deps.size(); ++i) {
        if (std::find(finished.begin(), finished.end(), deps[i]) == finished.end()) {
            missing = missing + 1;
        }
    }
    return missing;
}

std::vector<int> DependencyGraph::roots() const {
    std::vector<int> out;
    for (size_t i = 0; i < order_added_.size(); ++i) {
        int id = order_added_[i];
        if (dependencies_of(id).empty()) {
            out.push_back(id);
        }
    }
    return out;
}

// mark: 0 unvisited, 1 on stack, 2 finished.
bool DependencyGraph::visit(int id, std::map<int, int> &mark, std::vector<int> &out) const {
    int state = mark[id];
    if (state == 1) {
        return false;  // cycle
    }
    if (state == 2) {
        return true;
    }
    mark[id] = 1;
    const std::vector<int> &deps = dependencies_of(id);
    for (size_t i = 0; i < deps.size(); ++i) {
        if (!has_task(deps[i])) {
            continue;
        }
        if (!visit(deps[i], mark, out)) {
            return false;
        }
    }
    mark[id] = 2;
    out.push_back(id);
    return true;
}

bool DependencyGraph::topological_order(std::vector<int> &out) const {
    out.clear();
    std::map<int, int> mark;
    for (size_t i = 0; i < order_added_.size(); ++i) {
        if (!visit(order_added_[i], mark, out)) {
            out.clear();
            return false;
        }
    }
    return true;
}

std::vector<std::vector<int> > DependencyGraph::levels() const {
    std::vector<std::vector<int> > out;
    std::vector<int> order;
    if (!topological_order(order)) {
        return out;
    }

    std::map<int, int> depth;
    for (size_t i = 0; i < order.size(); ++i) {
        int id = order[i];
        const std::vector<int> &deps = dependencies_of(id);
        int d = 0;
        for (size_t j = 0; j < deps.size(); ++j) {
            std::map<int, int>::const_iterator found = depth.find(deps[j]);
            if (found != depth.end() && found->second + 1 > d) {
                d = found->second + 1;
            }
        }
        depth[id] = d;
        while (static_cast<int>(out.size()) <= d) {
            out.push_back(std::vector<int>());
        }
        out[d].push_back(id);
    }
    return out;
}

int DependencyGraph::critical_depth() const {
    std::vector<std::vector<int> > lv = levels();
    return static_cast<int>(lv.size());
}

bool dependencies_resolvable(const std::vector<Task> &tasks) {
    std::vector<int> ids;
    for (size_t i = 0; i < tasks.size(); ++i) {
        ids.push_back(tasks[i].id);
    }
    for (size_t i = 0; i < tasks.size(); ++i) {
        const std::vector<int> &deps = tasks[i].deps;
        for (size_t j = 0; j < deps.size(); ++j) {
            if (std::find(ids.begin(), ids.end(), deps[j]) == ids.end()) {
                return false;
            }
        }
    }
    return true;
}

DependencyGraph build_graph(const std::vector<Task> &tasks) {
    DependencyGraph graph;
    for (size_t i = 0; i < tasks.size(); ++i) {
        graph.add_task(tasks[i]);
    }
    return graph;
}

}  // namespace taskflow
