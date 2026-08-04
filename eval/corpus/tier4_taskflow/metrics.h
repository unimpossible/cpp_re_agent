// Aggregation of run records into a small report.
#ifndef TASKFLOW_METRICS_H
#define TASKFLOW_METRICS_H

#include <string>
#include <vector>

#include "task.h"

namespace taskflow {

struct Summary {
    int completed;
    int failed;
    int total_elapsed;
    int longest;
    int shortest;
    int retries;
};

class MetricsCollector {
public:
    MetricsCollector();

    void record(const RunRecord &record);
    void reset();               // same method name as Scheduler::reset
    void reset(int keep_last);  // overload: keep the most recent N records

    size_t count() const;
    size_t size() const;        // same method name as DependencyGraph::size
    Summary summarize() const;

    // Nearest-rank percentile over elapsed times; 0 when nothing recorded.
    int percentile(int pct) const;
    int mean_elapsed() const;

private:
    std::vector<RunRecord> records_;

    std::vector<int> sorted_elapsed() const;
};

std::string format_summary(const Summary &summary);

// Overload set: same name, three different parameter types. A decompiled
// binary has three distinct functions here that all demangle to "describe".
std::string describe(int elapsed);
std::string describe(const RunRecord &record);
std::string describe(const Summary &summary);

}  // namespace taskflow

#endif
