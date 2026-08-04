#include "workload.h"

#include <sstream>

namespace taskflow {

static Task make_task(int id, const char *name, Priority priority, int cost) {
    return Task(id, std::string(name), priority, cost);
}

void add_fanout(std::vector<Task> &tasks, int parent_id, int count, int base_cost) {
    int next_id = 0;
    for (size_t i = 0; i < tasks.size(); ++i) {
        if (tasks[i].id > next_id) {
            next_id = tasks[i].id;
        }
    }

    for (int i = 0; i < count; ++i) {
        next_id = next_id + 1;
        std::ostringstream name;
        name << "shard_" << i;
        Task task(next_id, name.str(), Priority::Normal, base_cost + i * 2);
        task.deps.push_back(parent_id);
        tasks.push_back(task);
    }
}

std::vector<Task> build_workload() {
    std::vector<Task> tasks;

    tasks.push_back(make_task(1, "fetch_sources", Priority::High, 4));
    tasks.push_back(make_task(2, "fetch_deps", Priority::High, 6));
    tasks.push_back(make_task(3, "configure", Priority::Normal, 3));
    tasks.push_back(make_task(4, "compile_core", Priority::Critical, 12));
    tasks.push_back(make_task(5, "compile_ui", Priority::Normal, 9));
    tasks.push_back(make_task(6, "link", Priority::Critical, 5));
    tasks.push_back(make_task(7, "unit_tests", Priority::High, 8));
    tasks.push_back(make_task(8, "integration_tests", Priority::Normal, 14));
    tasks.push_back(make_task(9, "package", Priority::Low, 2));

    tasks[2].deps.push_back(1);
    tasks[2].deps.push_back(2);
    tasks[3].deps.push_back(3);
    tasks[4].deps.push_back(3);
    tasks[5].deps.push_back(4);
    tasks[5].deps.push_back(5);
    tasks[6].deps.push_back(6);
    tasks[7].deps.push_back(6);
    tasks[7].deps.push_back(7);
    tasks[8].deps.push_back(8);

    add_fanout(tasks, 4, 3, 5);
    return tasks;
}

std::vector<Task> expensive_tasks(const std::vector<Task> &tasks, int threshold) {
    std::vector<Task> out;
    for (size_t i = 0; i < tasks.size(); ++i) {
        if (tasks[i].cost >= threshold) {
            out.push_back(tasks[i]);
        }
    }
    return out;
}

std::string join_names(const std::vector<Task> &tasks) {
    std::string out;
    for (size_t i = 0; i < tasks.size(); ++i) {
        if (i > 0) {
            out += ", ";
        }
        out += tasks[i].name;
    }
    return out;
}

}  // namespace taskflow
