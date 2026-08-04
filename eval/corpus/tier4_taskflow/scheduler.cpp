#include "scheduler.h"

#include <algorithm>
#include <sstream>

namespace taskflow {

Lcg::Lcg(unsigned int seed) : state_(seed ? seed : 1u) {}

unsigned int Lcg::next() {
    state_ = state_ * 1664525u + 1013904223u;
    return state_;
}

int Lcg::next_in_range(int lo, int hi) {
    if (hi <= lo) {
        return lo;
    }
    unsigned int span = static_cast<unsigned int>(hi - lo + 1);
    return lo + static_cast<int>(next() % span);
}

SchedulerConfig default_config() {
    SchedulerConfig config;
    config.max_attempts = 3;
    config.failure_modulus = 7;
    config.seed = 20240517u;
    return config;
}

Scheduler::Scheduler(const std::vector<Task> &tasks, const SchedulerConfig &config)
    : tasks_(tasks), graph_(build_graph(tasks)), config_(config),
      metrics_(), finished_(), tick_(0) {}

Task *Scheduler::mutable_task(int id) {
    for (size_t i = 0; i < tasks_.size(); ++i) {
        if (tasks_[i].id == id) {
            return &tasks_[i];
        }
    }
    return 0;
}

const Task *Scheduler::find_task(int id) const {
    for (size_t i = 0; i < tasks_.size(); ++i) {
        if (tasks_[i].id == id) {
            return &tasks_[i];
        }
    }
    return 0;
}

bool Scheduler::is_runnable(const Task &task) const {
    if (task.state == State::Done || task.state == State::Blocked) {
        return false;
    }
    return graph_.unmet_count(task.id, finished_) == 0;
}

std::vector<int> Scheduler::ready_ids() const {
    std::vector<Task> candidates;
    for (size_t i = 0; i < tasks_.size(); ++i) {
        if (is_runnable(tasks_[i])) {
            candidates.push_back(tasks_[i]);
        }
    }
    std::sort(candidates.begin(), candidates.end(), task_is_more_urgent);

    std::vector<int> ids;
    for (size_t i = 0; i < candidates.size(); ++i) {
        ids.push_back(candidates[i].id);
    }
    return ids;
}

RunRecord Scheduler::execute(Task &task, Lcg &rng) {
    RunRecord record;
    record.task_id = task.id;
    record.attempts = 0;
    record.elapsed = 0;
    record.ok = false;

    task.state = State::Running;
    while (record.attempts < config_.max_attempts) {
        record.attempts = record.attempts + 1;
        task.attempts = task.attempts + 1;
        tick_ = tick_ + 1;

        int jitter = rng.next_in_range(0, 3);
        record.elapsed += task.cost + jitter;

        bool doomed = (tick_ % config_.failure_modulus) == 0;
        if (!doomed || record.attempts >= config_.max_attempts) {
            record.ok = !doomed;
            break;
        }
        task.state = State::Failed;
    }

    task.state = record.ok ? State::Done : State::Failed;
    return record;
}

void Scheduler::mark_blocked_tasks() {
    for (size_t i = 0; i < tasks_.size(); ++i) {
        Task &task = tasks_[i];
        if (task.state == State::Done || task.state == State::Blocked) {
            continue;
        }
        if (graph_.unmet_count(task.id, finished_) > 0) {
            task.state = State::Blocked;
        }
    }
}

int Scheduler::run() {
    Lcg rng(config_.seed);
    int completed = 0;

    std::vector<int> order;
    if (!graph_.topological_order(order)) {
        return 0;  // cyclic dependencies: refuse to run anything
    }

    for (size_t pass = 0; pass < tasks_.size(); ++pass) {
        std::vector<int> ready = ready_ids();
        if (ready.empty()) {
            break;
        }

        bool progressed = false;
        for (size_t i = 0; i < ready.size(); ++i) {
            Task *task = mutable_task(ready[i]);
            if (task == 0 || task->state == State::Done) {
                continue;
            }
            RunRecord record = execute(*task, rng);
            metrics_.record(record);
            if (record.ok) {
                finished_.push_back(task->id);
                completed = completed + 1;
            }
            progressed = true;
        }
        if (!progressed) {
            break;
        }
    }

    mark_blocked_tasks();
    return completed;
}

void Scheduler::reset() {
    for (size_t i = 0; i < tasks_.size(); ++i) {
        tasks_[i].state = State::Pending;
        tasks_[i].attempts = 0;
    }
    finished_.clear();
    metrics_.reset();
    tick_ = 0;
}

const MetricsCollector &Scheduler::metrics() const {
    return metrics_;
}

std::vector<Task> Scheduler::tasks_in_state(State state) const {
    std::vector<Task> out;
    for (size_t i = 0; i < tasks_.size(); ++i) {
        if (tasks_[i].state == state) {
            out.push_back(tasks_[i]);
        }
    }
    return out;
}

std::string Scheduler::report() const {
    Summary summary = metrics_.summarize();
    std::ostringstream out;
    out << format_summary(summary) << "\n";
    out << "depth=" << graph_.critical_depth()
        << " mean=" << metrics_.mean_elapsed()
        << " p90=" << metrics_.percentile(90) << "\n";
    return out.str();
}

}  // namespace taskflow
