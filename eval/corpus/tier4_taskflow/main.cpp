// Entry point: build a workload, validate it, schedule it, print a report.
#include <iostream>
#include <vector>

#include "graph.h"
#include "metrics.h"
#include "scheduler.h"
#include "task.h"
#include "workload.h"

using namespace taskflow;

static void print_levels(const DependencyGraph &graph) {
    std::vector<std::vector<int> > levels = graph.levels();
    for (size_t i = 0; i < levels.size(); ++i) {
        std::cout << "level " << i << ":";
        for (size_t j = 0; j < levels[i].size(); ++j) {
            std::cout << " " << levels[i][j];
        }
        std::cout << "\n";
    }
}

static int report_states(const Scheduler &scheduler) {
    const State states[] = {State::Done, State::Failed, State::Blocked};
    int accounted = 0;
    for (int i = 0; i < 3; ++i) {
        std::vector<Task> group = scheduler.tasks_in_state(states[i]);
        accounted += static_cast<int>(group.size());
        std::cout << state_name(states[i]) << "=" << group.size();
        if (!group.empty()) {
            std::cout << " [" << join_names(group) << "]";
        }
        std::cout << "\n";
    }
    return accounted;
}

int main() {
    std::vector<Task> tasks = build_workload();

    if (!dependencies_resolvable(tasks)) {
        std::cout << "workload has unresolvable dependencies\n";
        return 1;
    }

    DependencyGraph graph = build_graph(tasks);
    std::vector<int> order;
    if (!graph.topological_order(order)) {
        std::cout << "workload has a dependency cycle\n";
        return 1;
    }

    std::cout << "tasks=" << tasks.size()
              << " cost=" << total_cost(tasks)
              << " roots=" << graph.roots().size()
              << " depth=" << graph.critical_depth() << "\n";
    print_levels(graph);

    std::vector<Task> heavy = expensive_tasks(tasks, 8);
    std::cout << "heavy=" << heavy.size() << " [" << join_names(heavy) << "]\n";

    Scheduler scheduler(tasks, default_config());
    int completed = scheduler.run();
    std::cout << "completed=" << completed << "\n";
    std::cout << scheduler.report();

    int accounted = report_states(scheduler);
    std::cout << "accounted=" << accounted << "\n";

    // Exercise the overload set and both classes' reset().
    Summary summary = scheduler.metrics().summarize();
    std::cout << "describe/summary: " << describe(summary) << "\n";
    std::cout << "describe/int: " << describe(summary.total_elapsed) << "\n";
    RunRecord probe;
    probe.task_id = 4;
    probe.elapsed = summary.longest;
    probe.attempts = 1;
    probe.ok = true;
    std::cout << "describe/record: " << describe(probe) << "\n";
    std::cout << "metrics size=" << scheduler.metrics().size() << "\n";

    const Task *core = scheduler.find_task(4);
    if (core != 0) {
        std::cout << "core: " << core->name
                  << " priority=" << priority_name(core->priority)
                  << " state=" << state_name(core->state)
                  << " attempts=" << core->attempts << "\n";
    }
    return 0;
}
