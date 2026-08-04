// Builds the fixed task workload the program schedules.
#ifndef TASKFLOW_WORKLOAD_H
#define TASKFLOW_WORKLOAD_H

#include <string>
#include <vector>

#include "task.h"

namespace taskflow {

// A deterministic build-pipeline-shaped workload: fetch -> compile -> link -> test.
std::vector<Task> build_workload();

// Adds `count` synthetic fan-out tasks that all depend on `parent_id`.
void add_fanout(std::vector<Task> &tasks, int parent_id, int count, int base_cost);

// Tasks whose declared cost is at or above `threshold`.
std::vector<Task> expensive_tasks(const std::vector<Task> &tasks, int threshold);

// Names joined with ", " — used by the report.
std::string join_names(const std::vector<Task> &tasks);

}  // namespace taskflow

#endif
