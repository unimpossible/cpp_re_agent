// Task records and small value types shared across the program.
#ifndef TASKFLOW_TASK_H
#define TASKFLOW_TASK_H

#include <string>
#include <vector>

namespace taskflow {

enum class Priority {
    Low = 0,
    Normal = 1,
    High = 2,
    Critical = 3,
};

enum class State {
    Pending = 0,
    Ready = 1,
    Running = 2,
    Done = 3,
    Failed = 4,
    Blocked = 5,
};

// One unit of work. `deps` holds the ids of tasks that must finish first.
struct Task {
    int id;
    std::string name;
    Priority priority;
    State state;
    int cost;               // synthetic duration units
    int attempts;
    std::vector<int> deps;

    Task();
    Task(int id_, const std::string &name_, Priority priority_, int cost_);
};

// Outcome of running a single task.
struct RunRecord {
    int task_id;
    int elapsed;
    int attempts;
    bool ok;
};

const char *priority_name(Priority p);
const char *state_name(State s);

// Ordering helper: higher priority first, then lower cost, then id.
bool task_is_more_urgent(const Task &a, const Task &b);

// A task is retryable if it failed but has attempts left.
bool task_can_retry(const Task &task, int max_attempts);

// Total declared cost of a set of tasks.
int total_cost(const std::vector<Task> &tasks);

}  // namespace taskflow

#endif
