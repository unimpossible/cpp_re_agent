// Runs tasks in dependency order, with retries and a metrics side-channel.
#ifndef TASKFLOW_SCHEDULER_H
#define TASKFLOW_SCHEDULER_H

#include <map>
#include <string>
#include <vector>

#include "graph.h"
#include "metrics.h"
#include "task.h"

namespace taskflow {

// Deterministic stand-in for real work: a seeded LCG, so a run is reproducible.
class Lcg {
public:
    explicit Lcg(unsigned int seed);
    unsigned int next();
    int next_in_range(int lo, int hi);

private:
    unsigned int state_;
};

struct SchedulerConfig {
    int max_attempts;
    int failure_modulus;   // every Nth simulated run fails once
    unsigned int seed;
};

SchedulerConfig default_config();

class Scheduler {
public:
    Scheduler(const std::vector<Task> &tasks, const SchedulerConfig &config);

    // Runs everything runnable; returns the number of tasks that finished.
    int run();

    // Same method name as MetricsCollector::reset — distinguishable only by
    // its owning class once decompiled.
    void reset();

    const MetricsCollector &metrics() const;
    std::vector<Task> tasks_in_state(State state) const;
    const Task *find_task(int id) const;
    std::string report() const;

private:
    std::vector<Task> tasks_;
    DependencyGraph graph_;
    SchedulerConfig config_;
    MetricsCollector metrics_;
    std::vector<int> finished_;
    int tick_;

    Task *mutable_task(int id);
    bool is_runnable(const Task &task) const;
    RunRecord execute(Task &task, Lcg &rng);
    void mark_blocked_tasks();
    std::vector<int> ready_ids() const;
};

}  // namespace taskflow

#endif
