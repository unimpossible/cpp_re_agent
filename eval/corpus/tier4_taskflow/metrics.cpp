#include "metrics.h"

#include <algorithm>
#include <sstream>

namespace taskflow {

MetricsCollector::MetricsCollector() : records_() {}

void MetricsCollector::record(const RunRecord &record) {
    records_.push_back(record);
}

void MetricsCollector::reset() {
    records_.clear();
}

void MetricsCollector::reset(int keep_last) {
    if (keep_last <= 0) {
        records_.clear();
        return;
    }
    if (static_cast<int>(records_.size()) <= keep_last) {
        return;
    }
    std::vector<RunRecord> kept;
    size_t start = records_.size() - static_cast<size_t>(keep_last);
    for (size_t i = start; i < records_.size(); ++i) {
        kept.push_back(records_[i]);
    }
    records_ = kept;
}

size_t MetricsCollector::count() const {
    return records_.size();
}

size_t MetricsCollector::size() const {
    return records_.size();
}

std::vector<int> MetricsCollector::sorted_elapsed() const {
    std::vector<int> values;
    for (size_t i = 0; i < records_.size(); ++i) {
        values.push_back(records_[i].elapsed);
    }
    std::sort(values.begin(), values.end());
    return values;
}

Summary MetricsCollector::summarize() const {
    Summary summary;
    summary.completed = 0;
    summary.failed = 0;
    summary.total_elapsed = 0;
    summary.longest = 0;
    summary.shortest = 0;
    summary.retries = 0;

    if (records_.empty()) {
        return summary;
    }

    summary.shortest = records_[0].elapsed;
    for (size_t i = 0; i < records_.size(); ++i) {
        const RunRecord &r = records_[i];
        if (r.ok) {
            summary.completed = summary.completed + 1;
        } else {
            summary.failed = summary.failed + 1;
        }
        summary.total_elapsed += r.elapsed;
        if (r.elapsed > summary.longest) {
            summary.longest = r.elapsed;
        }
        if (r.elapsed < summary.shortest) {
            summary.shortest = r.elapsed;
        }
        if (r.attempts > 1) {
            summary.retries += r.attempts - 1;
        }
    }
    return summary;
}

int MetricsCollector::percentile(int pct) const {
    if (records_.empty()) {
        return 0;
    }
    if (pct < 0) {
        pct = 0;
    }
    if (pct > 100) {
        pct = 100;
    }
    std::vector<int> values = sorted_elapsed();
    int rank = (pct * static_cast<int>(values.size()) + 99) / 100;
    if (rank < 1) {
        rank = 1;
    }
    return values[static_cast<size_t>(rank - 1)];
}

int MetricsCollector::mean_elapsed() const {
    if (records_.empty()) {
        return 0;
    }
    int total = 0;
    for (size_t i = 0; i < records_.size(); ++i) {
        total += records_[i].elapsed;
    }
    return total / static_cast<int>(records_.size());
}

std::string describe(int elapsed) {
    std::ostringstream out;
    out << "elapsed(" << elapsed << ")";
    return out.str();
}

std::string describe(const RunRecord &record) {
    std::ostringstream out;
    out << "task#" << record.task_id
        << (record.ok ? " ok" : " failed")
        << " in " << record.elapsed
        << " over " << record.attempts << " attempt(s)";
    return out.str();
}

std::string describe(const Summary &summary) {
    std::ostringstream out;
    out << summary.completed << " done, " << summary.failed
        << " failed, " << summary.total_elapsed << " total";
    return out.str();
}

std::string format_summary(const Summary &summary) {
    std::ostringstream out;
    out << "completed=" << summary.completed
        << " failed=" << summary.failed
        << " elapsed=" << summary.total_elapsed
        << " longest=" << summary.longest
        << " shortest=" << summary.shortest
        << " retries=" << summary.retries;
    return out.str();
}

}  // namespace taskflow
