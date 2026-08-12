// ============================================================================
//  小念意识层 C++ 加速核心 (pyclayer)
// ----------------------------------------------------------------------------
//  用 C++ 重写意识层"计算热点"（关联图扩散激活 / 强度 / 记忆存储），
//  算法与 Python 原版 (assoc_graph.py / memory_store.py) 逐位对齐，
//  以保证"类人意识逻辑功能不变"，同时更快、可并行。
//
//  构建：见同目录 build_pyclayer.bat / build_pyclayer.sh
//  常量：cpp_config.h 由 gen_cpp_config.py 从 cl_config.py 自动生成。
// ============================================================================
#include "cpp_config.h"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace py = pybind11;

// ------------------------- 并行调度（轻量线程池） -------------------------
static std::atomic<bool> g_parallel{true};

// 简单并行 for：把 [0,n) 按线程数切片。n 较小时退化为串行，避免线程开销。
template <typename F>
void parallel_for(std::size_t n, F f) {
    if (!g_parallel || n < 256) {
        for (std::size_t i = 0; i < n; ++i) f(i, 0, 1u);
        return;
    }
    unsigned nt = std::thread::hardware_concurrency();
    if (nt < 1) nt = 1;
    if (nt > n) nt = static_cast<unsigned>(n);
    std::vector<std::thread> ths;
    ths.reserve(nt);
    for (unsigned t = 0; t < nt; ++t) {
        ths.emplace_back([&, t, nt]() {
            for (std::size_t i = t; i < n; i += nt) f(i, t, nt);
        });
    }
    for (auto& th : ths) th.join();
}

// ============================== MemoryStore ==============================
// 对应 memory_store.py：有上限的信息单元库 + 统计计数 + 近因 + 信息增量。
struct MemoryStore {
    long capacity = STORAGE_CAPACITY;
    std::unordered_map<std::string, double> counts;
    std::unordered_map<std::string, long> last_seen;
    std::unordered_map<std::string, double> info_delta;
    long clock_ = 0;

    MemoryStore(long cap = STORAGE_CAPACITY) : capacity(cap) {}

    void observe(const std::string& concept, double amount = REINFORCE_NODE) {
        if (concept.empty()) return;
        counts[concept] = counts[concept] + amount;
        last_seen[concept] = clock_;
    }
    void observe_many(const std::vector<std::string>& concepts, double amount = REINFORCE_NODE) {
        for (const auto& c : concepts) observe(c, amount);
    }
    std::vector<std::string> tick() {
        clock_ += 1;
        double decay = DECAY_PER_TURN;
        for (auto& kv : counts) kv.second *= decay;
        return enforce_capacity();
    }
    double count(const std::string& concept) const {
        auto it = counts.find(concept);
        return it == counts.end() ? 0.0 : it->second;
    }
    double salience(const std::string& concept) const {
        double c = count(concept);
        return std::log1p(c > 0.0 ? c : 0.0);
    }
    double total_observations() const {
        double s = 0.0;
        for (auto& kv : counts) s += kv.second;
        return s;
    }
    std::size_t size() const { return counts.size(); }
    bool contains(const std::string& concept) const { return counts.count(concept) > 0; }
    double recency(const std::string& concept) const {
        auto it = last_seen.find(concept);
        if (it == last_seen.end()) return 0.0;
        long age = clock_ - it->second;
        return std::exp(-static_cast<double>(age) / std::max(1e-6, RECENCY_TAU));
    }
    double recent_info_increase(const std::string& concept) const {
        auto it = info_delta.find(concept);
        return it == info_delta.end() ? 0.0 : it->second;
    }
    std::unordered_map<std::string, double> snapshot_counts() const { return counts; }
    void commit_deltas(const std::unordered_map<std::string, double>& prev) {
        info_delta.clear();
        for (auto& kv : counts) {
            auto pit = prev.find(kv.first);
            double d = kv.second - (pit == prev.end() ? 0.0 : pit->second);
            if (d > 1e-9) info_delta[kv.first] = d;
        }
    }
    std::vector<std::pair<std::string, double>> top(int n = 20) const {
        std::vector<std::pair<std::string, double>> v(counts.begin(), counts.end());
        std::sort(v.begin(), v.end(),
                  [](const auto& a, const auto& b) { return a.second > b.second; });
        if (static_cast<int>(v.size()) > n) v.resize(n);
        return v;
    }

    std::vector<std::string> enforce_capacity() {
        long over = static_cast<long>(counts.size()) - capacity;
        if (over <= 0) return {};
        std::vector<std::pair<std::string, double>> ordered(counts.begin(), counts.end());
        std::sort(ordered.begin(), ordered.end(),
                  [](const auto& a, const auto& b) { return a.second < b.second; });
        std::vector<std::string> evicted;
        for (auto& kv : ordered) {
            if (over <= 0) break;
            counts.erase(kv.first);
            last_seen.erase(kv.first);
            evicted.push_back(kv.first);
            over -= 1;
        }
        return evicted;
    }

    py::dict to_dict() const {
        py::dict d;
        d["capacity"] = capacity;
        py::dict cdict, ldict, idict;
        for (auto& kv : counts) cdict[py::cast(kv.first)] = kv.second;
        for (auto& kv : last_seen) ldict[py::cast(kv.first)] = kv.second;
        for (auto& kv : info_delta) idict[py::cast(kv.first)] = kv.second;
        d["counts"] = cdict;
        d["last_seen"] = ldict;
        d["info_delta"] = idict;
        d["clock"] = clock_;
        return d;
    }
    static MemoryStore from_dict(py::dict d) {
        MemoryStore obj;
        obj.capacity = d.contains("capacity") ? py::cast<long>(d["capacity"]) : STORAGE_CAPACITY;
        py::dict cd = py::cast<py::dict>(d["counts"]);
        for (auto& kv : cd) obj.counts[py::cast<std::string>(kv.first)] = py::cast<double>(kv.second);
        py::dict ld = py::cast<py::dict>(d["last_seen"]);
        for (auto& kv : ld) obj.last_seen[py::cast<std::string>(kv.first)] = py::cast<long>(kv.second);
        if (d.contains("info_delta")) {
            py::dict id = py::cast<py::dict>(d["info_delta"]);
            for (auto& kv : id) obj.info_delta[py::cast<std::string>(kv.first)] = py::cast<double>(kv.second);
        }
        obj.clock_ = d.contains("clock") ? py::cast<long>(d["clock"]) : 0;
        return obj;
    }

    // ---------- 供 Python 上层直接访问兼容（意识层 consciousness.py 用） ----------
    // stl.h 默认把 unordered_map 作为拷贝转换，就地 setdefault/pop 不会写回 C++ 对象，
    // 故显式提供这些方法，让上层无感访问内部字段。
    py::dict get_counts() const {
        py::dict d;
        for (auto& kv : counts) d[py::cast(kv.first)] = kv.second;
        return d;
    }
    void set_counts(py::dict d) {
        counts.clear();
        for (auto& kv : d) counts[py::cast<std::string>(kv.first)] = py::cast<double>(kv.second);
    }
    py::dict get_last_seen() const {
        py::dict d;
        for (auto& kv : last_seen) d[py::cast(kv.first)] = kv.second;
        return d;
    }
    void set_last_seen(py::dict d) {
        last_seen.clear();
        for (auto& kv : d) last_seen[py::cast<std::string>(kv.first)] = py::cast<long>(kv.second);
    }
    void pop_count(const std::string& c) { counts.erase(c); }
    void pop_last_seen(const std::string& c) { last_seen.erase(c); }
};

// ============================== AssocGraph ==============================
// 对应 assoc_graph.py：无向加权关联图 + 扩散激活 + 强度维度 + 压缩整合。
struct AssocGraph {
    // a -> {b: co_count}
    std::unordered_map<std::string, std::unordered_map<std::string, double>> edges;
    std::unordered_map<std::string, double> strength;
    std::unordered_map<std::string, double> assoc_count;
    std::unordered_map<std::string, double> similar_count;
    std::unordered_map<std::string, long> last_active;
    long turn = 0;

    // ---------- 构建 / 统计 ----------
    void touch(const std::string& concept, long t = -1) {
        if (concept.empty()) return;
        last_active[concept] = (t >= 0) ? t : turn;
    }
    void touch_many(const std::vector<std::string>& concepts) {
        for (const auto& c : concepts) touch(c);
    }
    double get_edge(const std::string& a, const std::string& b) const {
        auto it = edges.find(a);
        if (it == edges.end()) return 0.0;
        auto jt = it->second.find(b);
        return jt == it->second.end() ? 0.0 : jt->second;
    }
    void set_edge(const std::string& a, const std::string& b, double v) { edges[a][b] = v; }

    void link(const std::string& a, const std::string& b, double amount = REINFORCE_EDGE) {
        if (a.empty() || b.empty() || a == b) return;
        edges[a][b] = edges[a][b] + amount;
        edges[b][a] = edges[b][a] + amount;
        prune_node(a);
        prune_node(b);
    }
    void link_group(const std::vector<std::string>& concepts, double amount = REINFORCE_EDGE) {
        std::vector<std::string> uniq;
        std::unordered_set<std::string> seen;
        for (const auto& c : concepts) {
            if (c.empty() || seen.count(c)) continue;
            seen.insert(c);
            uniq.push_back(c);
        }
        for (std::size_t i = 0; i < uniq.size(); ++i)
            for (std::size_t j = i + 1; j < uniq.size(); ++j)
                link(uniq[i], uniq[j], amount);
    }
    void link_sequence(const std::vector<std::string>& concepts, int window = PROXIMITY_WINDOW,
                       double base = REINFORCE_EDGE, double decay = PROXIMITY_DECAY) {
        std::vector<std::string> seq;
        for (const auto& c : concepts)
            if (!c.empty()) seq.push_back(c);
        long n = static_cast<long>(seq.size());
        for (long i = 0; i < n; ++i) {
            for (long d = 1; d <= window; ++d) {
                long j = i + d;
                if (j >= n) break;
                if (seq[i] == seq[j]) continue;
                link(seq[i], seq[j], base * std::pow(decay, d - 1));
            }
        }
    }
    // 衰减并修剪过弱边（含空邻居清理）。
    // 注意：erase 操作在两层容器上，必须用迭代器式循环，不可在 range-for 里 erase。
    void decay(double factor = DECAY_PER_TURN) {
        turn += 1;
        for (auto git = edges.begin(); git != edges.end();) {
            auto& nbrs = git->second;
            for (auto it = nbrs.begin(); it != nbrs.end();) {
                it->second *= factor;
                it = (it->second < 1e-4) ? nbrs.erase(it) : std::next(it);
            }
            git = nbrs.empty() ? edges.erase(git) : std::next(git);
        }
    }
    void prune_node(const std::string& a) {
        auto it = edges.find(a);
        if (it == edges.end()) return;
        if (static_cast<long>(it->second.size()) > EDGE_CAPACITY_PER_NODE) {
            std::vector<std::pair<std::string, double>> v(it->second.begin(), it->second.end());
            std::sort(v.begin(), v.end(),
                      [](const auto& x, const auto& y) { return x.second > y.second; });
            std::unordered_map<std::string, double> keep;
            for (int k = 0; k < EDGE_CAPACITY_PER_NODE && k < static_cast<int>(v.size()); ++k)
                keep[v[k].first] = v[k].second;
            edges[a] = std::move(keep);
        }
    }
    void drop(const std::string& concept) {
        auto it = edges.find(concept);
        if (it != edges.end()) {
            for (auto& kv : it->second) edges[kv.first].erase(concept);
            edges.erase(it);
        }
        strength.erase(concept);
        assoc_count.erase(concept);
        similar_count.erase(concept);
        last_active.erase(concept);
    }
    std::size_t node_count() const {
        std::unordered_set<std::string> nodes;
        for (auto& kv : edges)
            for (auto& x : kv.second) {
                nodes.insert(kv.first);
                nodes.insert(x.first);
            }
        for (auto& kv : strength) nodes.insert(kv.first);
        return nodes.size();
    }
    std::vector<std::string> enforce_node_cap(long cap) {
        if (cap <= 0) return {};
        std::unordered_set<std::string> nodes;
        for (auto& kv : edges) nodes.insert(kv.first);
        for (auto& kv : strength) nodes.insert(kv.first);
        if (static_cast<long>(nodes.size()) <= cap) return {};
        std::vector<std::pair<std::string, double>> activity;
        activity.reserve(nodes.size());
        for (const auto& n : nodes) {
            double a = 0.0;
            auto it = edges.find(n);
            if (it != edges.end())
                for (auto& x : it->second) a += x.second;
            activity.push_back({n, a});
        }
        std::sort(activity.begin(), activity.end(),
                  [](const auto& x, const auto& y) { return x.second < y.second; });
        long drop_n = static_cast<long>(nodes.size()) - cap;
        std::vector<std::string> evicted;
        for (auto& kv : activity) {
            if (drop_n <= 0) break;
            drop(kv.first);
            evicted.push_back(kv.first);
            drop_n -= 1;
        }
        return evicted;
    }

    // ---------- 近因强度衰减 ----------
    std::vector<std::string> decay_strength_by_recency(
        bool enabled = (STRENGTH_RECENCY_ENABLED != 0),
        double decay = STRENGTH_RECENCY_DECAY, long grace = STRENGTH_IDLE_GRACE,
        double threshold = STRENGTH_FORGET_THRESHOLD) {
        if (!enabled) return {};
        std::vector<std::string> forgotten;
        for (auto it = strength.begin(); it != strength.end();) {
            auto la = last_active.find(it->first);
            long idle = (la != last_active.end()) ? (turn - la->second) : turn;
            if (idle <= grace) {
                ++it;
                continue;
            }
            it->second *= decay;
            if (it->second < threshold) {
                std::string c = it->first;
                it = strength.erase(it);
                drop(c);
                forgotten.push_back(c);
            } else {
                ++it;
            }
        }
        return forgotten;
    }

    // ---------- 压缩整合 ----------
    double benchmark_ms() const {
        auto start = std::chrono::steady_clock::now();
        for (auto& kv : edges) {
            for (auto& x : kv.second) {
                volatile double v = x.second * 0.997;
                (void)v;
            }
        }
        for (auto& kv : strength) {
            volatile double v = kv.second * 0.992;
            (void)v;
        }
        for (auto& kv : edges) {
            double total = 0.0;
            for (auto& x : kv.second) total += x.second;
            (void)total;
        }
        auto end = std::chrono::steady_clock::now();
        double ms = std::chrono::duration<double, std::milli>(end - start).count();
        return ms * SLEEP_COST_K;
    }
    std::string strongest_neighbor(const std::string& c) const {
        auto it = edges.find(c);
        if (it == edges.end()) return "";
        const auto& nbrs = it->second;
        const std::string* best = nullptr;
        double bw = -1.0;
        for (auto& kv : nbrs) {
            if (kv.second > bw) {
                bw = kv.second;
                best = &kv.first;
            }
        }
        return best ? *best : "";
    }
    void merge_into(const std::string& c, const std::string& target) {
        if (target.empty() || target == c) return;
        auto it = edges.find(c);
        if (it == edges.end()) return;
        for (auto& kv : it->second) {
            if (kv.first == target) continue;
            link(target, kv.first, kv.second);
        }
        drop(c);
    }
    std::vector<std::pair<std::string, std::string>> consolidate(
        double strength_max = CONSOLIDATE_STRENGTH_MAX, long batch = CONSOLIDATE_BATCH) {
        std::vector<std::pair<std::string, double>> cands;
        for (auto& kv : strength)
            if (kv.second > 0.0 && kv.second < strength_max) cands.push_back({kv.first, kv.second});
        std::sort(cands.begin(), cands.end(),
                  [](const auto& a, const auto& b) { return a.second < b.second; });
        std::vector<std::pair<std::string, std::string>> merged;
        for (auto& kv : cands) {
            if (static_cast<long>(merged.size()) >= batch) break;
            if (strength[kv.first] >= strength_max) break;
            std::string tgt = strongest_neighbor(kv.first);
            if (tgt.empty() || tgt == kv.first) continue;
            merge_into(kv.first, tgt);
            merged.push_back({kv.first, tgt});
        }
        return merged;
    }

    // ---------- 边权重 ----------
    // 与 Python 版 assoc_graph.py:edge_weight 逐位对齐：
    // 当 a/b 不在 mem 词频中(如 [ACT_SIT] / 椅子等动作/场景词)导致 denom<=0 时，
    // 用 max(1e-6, co) 保底分母，而非直接返回 0——否则"记忆-动作闭环"永远哑火。
    double edge_weight(const std::string& a, const std::string& b, const MemoryStore& mem) const {
        double co = get_edge(a, b);
        if (co <= 0.0) return 0.0;
        double ca = mem.count(a);
        double cb = mem.count(b);
        double denom = ca + cb - co;
        if (denom <= 0.0) {
            if (co <= 0.0) return 0.0;
            denom = std::max(1e-6, co);       // 保底分母，对齐 Python 版
        }
        return co / denom;
    }
    std::vector<std::pair<std::string, double>> neighbors(const std::string& a) const {
        auto it = edges.find(a);
        if (it == edges.end()) return {};
        return std::vector<std::pair<std::string, double>>(it->second.begin(), it->second.end());
    }

    // ---------- 强度维度 ----------
    double similarity(const std::string& a, const std::string& b) const {
        auto ia = edges.find(a);
        auto ib = edges.find(b);
        if (ia == edges.end() || ib == edges.end()) return 0.0;
        const auto& na = ia->second;
        const auto& nb = ib->second;
        if (na.empty() || nb.empty()) return 0.0;
        std::size_t inter = 0;
        if (na.size() <= nb.size()) {
            for (auto& kv : na)
                if (nb.count(kv.first)) ++inter;
        } else {
            for (auto& kv : nb)
                if (na.count(kv.first)) ++inter;
        }
        std::size_t uni = na.size() + nb.size() - inter;
        return uni > 0 ? static_cast<double>(inter) / static_cast<double>(uni) : 0.0;
    }
    void compute_strength(const std::string& w) {
        auto it = edges.find(w);
        const auto& nbrs = (it != edges.end()) ? it->second
                                              : std::unordered_map<std::string, double>{};
        long assoc = 0;
        for (auto& kv : nbrs)
            if (kv.second >= STRENGTH_ASSOC_MIN) ++assoc;
        std::unordered_set<std::string> candidates;
        for (auto& kv : nbrs) {
            auto jt = edges.find(kv.first);
            if (jt != edges.end())
                for (auto& x : jt->second) candidates.insert(x.first);
        }
        candidates.erase(w);
        std::vector<std::string> cv(candidates.begin(), candidates.end());
        std::size_t nt = cv.empty() ? 1 : std::thread::hardware_concurrency();
        std::vector<long> local(nt, 0);
        parallel_for(cv.size(), [&](std::size_t i, unsigned t, unsigned /*nt_*/) {
            if (similarity(w, cv[i]) >= SIM_THRESHOLD) local[t] += 1;
        });
        long sim = 0;
        for (long v : local) sim += v;
        assoc_count[w] = static_cast<double>(assoc);
        similar_count[w] = static_cast<double>(sim);
        strength[w] = STRENGTH_K * (static_cast<double>(assoc) + static_cast<double>(sim));
    }
    void finalize_strength(const std::vector<std::string>& new_words) {
        for (const auto& w : new_words)
            if (!w.empty() && strength.find(w) == strength.end()) compute_strength(w);
    }
    void ensure_strengths() {
        for (auto& kv : edges)
            if (strength.find(kv.first) == strength.end()) compute_strength(kv.first);
    }
    double strength_of(const std::string& c) const {
        auto it = strength.find(c);
        return it == strength.end() ? 0.0 : it->second;
    }
    double strength_score(const std::string& c) const {
        return std::min(1.0, strength_of(c) / std::max(1e-9, STRENGTH_REF));
    }
    double event_strength(const std::vector<std::string>& words) const {
        long n = static_cast<long>(words.size());
        if (n == 0) return 0.0;
        double s = 0.0;
        for (const auto& w : words) s += strength_of(w);
        return s * (1.0 + COMPOSE_BONUS * static_cast<double>(n - 1));
    }

    // ---------- 匹配度（并行内层） ----------
    double match_degree(const std::string& c, const std::unordered_set<std::string>& seed_set) const {
        if (seed_set.count(c)) return 1.0;
        std::vector<std::string> sv(seed_set.begin(), seed_set.end());
        std::size_t nt = sv.empty() ? 1 : std::thread::hardware_concurrency();
        std::vector<double> la(nt, 0.0), ls(nt, 0.0);
        parallel_for(sv.size(), [&](std::size_t i, unsigned t, unsigned /*nt_*/) {
            const std::string& s = sv[i];
            double co = get_edge(c, s);
            double wa = co > 0.0 ? std::min(1.0, co / (co + 1.0)) : 0.0;
            double ws = similarity(c, s);
            if (wa > la[t]) la[t] = wa;
            if (ws > ls[t]) ls[t] = ws;
        });
        double best_assoc = 0.0, best_sim = 0.0;
        for (double v : la) best_assoc = std::max(best_assoc, v);
        for (double v : ls) best_sim = std::max(best_sim, v);
        return MATCH_ASSOC_W * best_assoc + (1.0 - MATCH_ASSOC_W) * best_sim;
    }

    // ---------- 链式解锁（扩散激活） ----------
    std::unordered_map<std::string, double> spread_activation(
        const std::unordered_map<std::string, double>& seed_map, const MemoryStore& mem,
        int hops = SPREAD_HOPS, double decay = SPREAD_DECAY,
        double threshold = SPREAD_THRESHOLD, int max_unlock = SPREAD_MAX_UNLOCK) const {
        std::unordered_set<std::string> seed_set;
        for (auto& kv : seed_map) seed_set.insert(kv.first);

        auto energy_of = [&](const std::string& c, double base_e) -> double {
            double info_b = seed_set.count(c)
                                ? 1.0
                                : std::min(1.0, mem.recent_info_increase(c) / INFO_DELTA_REF);
            double s = strength_score(c);
            double r = mem.recency(c);
            return base_e * (1.0 + INFO_W * info_b + STRENGTH_W * s + RECENCY_W * r);
        };

        std::unordered_map<std::string, double> activation;
        // (-energy, hop, node) 最大堆（按 -energy 越小 = energy 越大 先出）
        using PQE = std::tuple<double, int, std::string>;
        std::priority_queue<PQE, std::vector<PQE>, std::greater<PQE>> heap;

        for (auto& kv : seed_map) {
            const std::string& s = kv.first;
            double e = kv.second;
            const_cast<AssocGraph*>(this)->touch(s);
            double pe = energy_of(s, e);
            activation[s] = activation[s] + pe;
            heap.push(std::make_tuple(-pe, 0, s));
        }

        while (!heap.empty() && static_cast<long>(activation.size()) < max_unlock) {
            auto [neg_e, hop, node] = heap.top();
            heap.pop();
            double energy = -neg_e;
            if (hop >= hops || energy < threshold) continue;
            auto nit = edges.find(node);
            if (nit == edges.end()) continue;
            for (auto& nbkv : nit->second) {
                const std::string& nb = nbkv.first;
                if (seed_set.count(nb) == 0) {
                    if (match_degree(nb, seed_set) < MATCH_THRESHOLD) continue;
                }
                double w = edge_weight(node, nb, mem);
                if (w <= 0.0) continue;
                double passed = energy * w * decay;
                if (passed < threshold) continue;
                double pe = energy_of(nb, passed);
                auto it = activation.find(nb);
                if (it == activation.end() || pe > it->second + 1e-9) {
                    if (it == activation.end())
                        activation[nb] = pe;
                    else
                        it->second = pe;
                    const_cast<AssocGraph*>(this)->touch(nb);
                    heap.push(std::make_tuple(-pe, hop + 1, nb));
                }
            }
        }
        return activation;
    }

    // pybind11 友好的包装：接受 py::dict 种子 + MemoryStore 引用
    py::dict spread_activation_py(py::dict seeds, const MemoryStore& mem, int hops = SPREAD_HOPS,
                                  double decay = SPREAD_DECAY, double threshold = SPREAD_THRESHOLD,
                                  int max_unlock = SPREAD_MAX_UNLOCK) const {
        std::unordered_map<std::string, double> seed_map;
        for (auto& item : seeds)
            seed_map[py::cast<std::string>(item.first)] = py::cast<double>(item.second);
        auto act = spread_activation(seed_map, mem, hops, decay, threshold, max_unlock);
        py::dict out;
        for (auto& kv : act) out[py::cast(kv.first)] = kv.second;
        return out;
    }
    double match_degree_py(const std::string& c, py::set seed) const {
        std::unordered_set<std::string> s;
        for (auto& o : seed) s.insert(py::cast<std::string>(o));
        return match_degree(c, s);
    }

    // ---------- 持久化 ----------
    py::dict to_dict() const {
        py::dict d;
        py::dict ed;
        for (auto& kv : edges) {
            py::dict nbrs;
            for (auto& x : kv.second) nbrs[py::cast(x.first)] = x.second;
            ed[py::cast(kv.first)] = nbrs;
        }
        py::dict st, ac, sc, la;
        for (auto& kv : strength) st[py::cast(kv.first)] = kv.second;
        for (auto& kv : assoc_count) ac[py::cast(kv.first)] = kv.second;
        for (auto& kv : similar_count) sc[py::cast(kv.first)] = kv.second;
        for (auto& kv : last_active) la[py::cast(kv.first)] = kv.second;
        d["edges"] = ed;
        d["strength"] = st;
        d["assoc_count"] = ac;
        d["similar_count"] = sc;
        d["last_active"] = la;
        d["turn"] = turn;
        return d;
    }
    static AssocGraph from_dict(py::dict d) {
        AssocGraph obj;
        py::dict ed = py::cast<py::dict>(d["edges"]);
        for (auto& kv : ed) {
            std::string a = py::cast<std::string>(kv.first);
            py::dict nbrs = py::cast<py::dict>(kv.second);
            for (auto& x : nbrs) obj.edges[a][py::cast<std::string>(x.first)] = py::cast<double>(x.second);
        }
        auto load_map = [&](const char* key, std::unordered_map<std::string, double>& dst) {
            if (d.contains(key)) {
                py::dict m = py::cast<py::dict>(d[key]);
                for (auto& x : m) dst[py::cast<std::string>(x.first)] = py::cast<double>(x.second);
            }
        };
        load_map("strength", obj.strength);
        load_map("assoc_count", obj.assoc_count);
        load_map("similar_count", obj.similar_count);
        if (d.contains("last_active")) {
            py::dict m = py::cast<py::dict>(d["last_active"]);
            for (auto& x : m) obj.last_active[py::cast<std::string>(x.first)] = py::cast<long>(x.second);
        }
        obj.turn = d.contains("turn") ? py::cast<long>(d["turn"]) : 0;
        if (obj.strength.empty() && !obj.edges.empty()) obj.ensure_strengths();
        return obj;
    }

    // ---------- 供 Python 上层直接访问兼容（意识层 consciousness.py 用） ----------
    py::dict snapshot_strength() const {
        py::dict d;
        for (auto& kv : strength) d[py::cast(kv.first)] = kv.second;
        return d;
    }
    void ensure_strength(const std::string& name, double default_val) {
        if (strength.find(name) == strength.end()) strength[name] = default_val;
    }
    void ensure_edge_slot(const std::string& a) {
        edges[a];  // 确保内层空 map 存在（等价 Python 的 edges.setdefault(a, {})）
    }
};

// ============================== pybind11 绑定 ==============================
PYBIND11_MODULE(pyclayer, m) {
    m.attr("cpp_version") = "1.0.0";

    py::class_<MemoryStore>(m, "MemoryStore")
        .def(py::init<long>(), py::arg("capacity") = STORAGE_CAPACITY)
        .def("observe", &MemoryStore::observe, py::arg("concept"), py::arg("amount") = REINFORCE_NODE)
        .def("observe_many", &MemoryStore::observe_many, py::arg("concepts"),
             py::arg("amount") = REINFORCE_NODE)
        .def("tick", &MemoryStore::tick)
        .def("count", &MemoryStore::count)
        .def("salience", &MemoryStore::salience)
        .def("total_observations", &MemoryStore::total_observations)
        .def("size", &MemoryStore::size)
        .def("contains", &MemoryStore::contains)
        .def("recency", &MemoryStore::recency)
        .def("recent_info_increase", &MemoryStore::recent_info_increase)
        .def("snapshot_counts", &MemoryStore::snapshot_counts)
        .def("commit_deltas", &MemoryStore::commit_deltas)
        .def("top", &MemoryStore::top, py::arg("n") = 20)
        .def("to_dict", &MemoryStore::to_dict)
        .def_static("from_dict", &MemoryStore::from_dict)
        .def("get_counts", &MemoryStore::get_counts)
        .def("set_counts", &MemoryStore::set_counts)
        .def("get_last_seen", &MemoryStore::get_last_seen)
        .def("set_last_seen", &MemoryStore::set_last_seen)
        .def("pop_count", &MemoryStore::pop_count)
        .def("pop_last_seen", &MemoryStore::pop_last_seen);

    py::class_<AssocGraph>(m, "AssocGraph")
        .def(py::init<>())
        .def("touch", &AssocGraph::touch, py::arg("concept"), py::arg("turn") = -1)
        .def("touch_many", &AssocGraph::touch_many)
        .def("link", &AssocGraph::link, py::arg("a"), py::arg("b"), py::arg("amount") = REINFORCE_EDGE)
        .def("link_group", &AssocGraph::link_group, py::arg("concepts"),
             py::arg("amount") = REINFORCE_EDGE)
        .def("link_sequence", &AssocGraph::link_sequence, py::arg("concepts"),
             py::arg("window") = PROXIMITY_WINDOW, py::arg("base") = REINFORCE_EDGE,
             py::arg("decay") = PROXIMITY_DECAY)
        .def("decay", &AssocGraph::decay, py::arg("factor") = DECAY_PER_TURN)
        .def("drop", &AssocGraph::drop)
        .def("node_count", &AssocGraph::node_count)
        .def("enforce_node_cap", &AssocGraph::enforce_node_cap)
        .def("decay_strength_by_recency", &AssocGraph::decay_strength_by_recency,
             py::arg("enabled") = (STRENGTH_RECENCY_ENABLED != 0),
             py::arg("decay") = STRENGTH_RECENCY_DECAY, py::arg("grace") = STRENGTH_IDLE_GRACE,
             py::arg("threshold") = STRENGTH_FORGET_THRESHOLD)
        .def("benchmark_ms", &AssocGraph::benchmark_ms)
        .def("strongest_neighbor", &AssocGraph::strongest_neighbor)
        .def("merge_into", &AssocGraph::merge_into)
        .def("consolidate", &AssocGraph::consolidate, py::arg("strength_max") = CONSOLIDATE_STRENGTH_MAX,
             py::arg("batch") = CONSOLIDATE_BATCH)
        .def("edge_weight", &AssocGraph::edge_weight)
        .def("neighbors", &AssocGraph::neighbors)
        .def("similarity", &AssocGraph::similarity)
        .def("finalize_strength", &AssocGraph::finalize_strength)
        .def("ensure_strengths", &AssocGraph::ensure_strengths)
        .def("strength_of", &AssocGraph::strength_of)
        .def("strength_score", &AssocGraph::strength_score)
        .def("event_strength", &AssocGraph::event_strength)
        .def("match_degree", &AssocGraph::match_degree_py)
        .def("spread_activation", &AssocGraph::spread_activation_py, py::arg("seeds"),
             py::arg("mem"), py::arg("hops") = SPREAD_HOPS, py::arg("decay") = SPREAD_DECAY,
             py::arg("threshold") = SPREAD_THRESHOLD, py::arg("max_unlock") = SPREAD_MAX_UNLOCK)
        .def("to_dict", &AssocGraph::to_dict)
        .def_static("from_dict", &AssocGraph::from_dict)
        .def("snapshot_strength", &AssocGraph::snapshot_strength)
        .def("ensure_strength", &AssocGraph::ensure_strength, py::arg("name"), py::arg("default_val"))
        .def("ensure_edge_slot", &AssocGraph::ensure_edge_slot);

    m.def("set_parallel", [](bool p) { g_parallel = p; });
    m.def("get_parallel", []() { return static_cast<bool>(g_parallel); });
}
