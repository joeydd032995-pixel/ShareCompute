#include "worker.h"
#include "ggml-backend.h"
#include "ggml-cpu.h"
#include "ggml-rpc.h"
#include <string>

int sc_worker_run(int port, uint64_t budget_bytes, int threads) {
    if (port < 1 || port > 65535 || budget_bytes < 64ULL*1024*1024 || threads < 1 || threads > 16) return 2;
    sc_rpc_set_budget(budget_bytes);
    auto reg = ggml_backend_cpu_reg();
    if (!reg || ggml_backend_reg_dev_count(reg) != 1) return 3;
    auto dev = ggml_backend_reg_dev_get(reg, 0);
    std::string endpoint = "127.0.0.1:" + std::to_string(port);
    ggml_backend_rpc_start_server(endpoint.c_str(), nullptr, threads, 1, &dev);
    return 4; // A healthy listener never returns.
}
uint64_t sc_worker_allocated(void) { return sc_rpc_allocated(); }
uint64_t sc_worker_peak(void) { return sc_rpc_peak(); }
uint64_t sc_worker_graphs(void) { return sc_rpc_graphs(); }
const char * sc_worker_revision(void) { return SC_REVISION; }
