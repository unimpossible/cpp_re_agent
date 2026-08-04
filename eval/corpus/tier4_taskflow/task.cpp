#include "task.h"

namespace taskflow {

Task::Task()
    : id(-1), name(), priority(Priority::Normal), state(State::Pending),
      cost(0), attempts(0), deps() {}

Task::Task(int id_, const std::string &name_, Priority priority_, int cost_)
    : id(id_), name(name_), priority(priority_), state(State::Pending),
      cost(cost_), attempts(0), deps() {}

const char *priority_name(Priority p) {
    switch (p) {
        case Priority::Low:      return "low";
        case Priority::Normal:   return "normal";
        case Priority::High:     return "high";
        case Priority::Critical: return "critical";
    }
    return "unknown";
}

const char *state_name(State s) {
    switch (s) {
        case State::Pending: return "pending";
        case State::Ready:   return "ready";
        case State::Running: return "running";
        case State::Done:    return "done";
        case State::Failed:  return "failed";
        case State::Blocked: return "blocked";
    }
    return "unknown";
}

bool task_is_more_urgent(const Task &a, const Task &b) {
    if (a.priority != b.priority) {
        return static_cast<int>(a.priority) > static_cast<int>(b.priority);
    }
    if (a.cost != b.cost) {
        return a.cost < b.cost;
    }
    return a.id < b.id;
}

bool task_can_retry(const Task &task, int max_attempts) {
    if (task.state != State::Failed) {
        return false;
    }
    return task.attempts < max_attempts;
}

int total_cost(const std::vector<Task> &tasks) {
    int sum = 0;
    for (size_t i = 0; i < tasks.size(); ++i) {
        sum += tasks[i].cost;
    }
    return sum;
}

}  // namespace taskflow
