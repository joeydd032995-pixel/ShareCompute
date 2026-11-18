#include "worker.h"
#include "ggml-backend.h"
#include "ggml-cpu.h"
#include "ggml-rpc.h"
#include <cstdio>
#include <filesystem>
#include <string>
#include <system_error>

#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__) || defined(_M_IX86)
#define SC_CPU_X86 1
#if defined(_MSC_VER)
#include <intrin.h>
#include <immintrin.h>
#else
#include <cpuid.h>
#endif
#elif defined(__aarch64__) || defined(_M_ARM64)
#define SC_CPU_ARM 1
#if defined(__APPLE__)
#include <sys/sysctl.h>
#elif defined(__linux__)
#include <sys/auxv.h>
#endif
#endif

// What the CPU can run, as opposed to what ggml_cpu_has_*() says the binary was compiled for.
// The two differ exactly when a build is wrong for its machine, which is the case worth logging.
static std::string sc_cpu_hardware() {
    std::string out;
    auto add = [&](const char * name, bool on) { if (on) { if (!out.empty()) out += ','; out += name; } };
#if defined(SC_CPU_X86)
    unsigned r[4] = {0, 0, 0, 0};
    auto cpuid = [&](unsigned leaf, unsigned sub) {
#if defined(_MSC_VER)
        int x[4]; __cpuidex(x, (int)leaf, (int)sub); for (int i = 0; i < 4; i++) r[i] = (unsigned)x[i];
#else
        __cpuid_count(leaf, sub, r[0], r[1], r[2], r[3]);
#endif
    };
    cpuid(0, 0); unsigned max_leaf = r[0];
    if (max_leaf < 1) return "unknown";
    cpuid(1, 0);
    bool osxsave = (r[2] >> 27) & 1, avx = (r[2] >> 28) & 1, fma = (r[2] >> 12) & 1, f16c = (r[2] >> 29) & 1;
    bool ymm = false;
    if (osxsave) {
        // AVX is only usable when the OS saves the YMM registers (XCR0 bits 1 and 2).
#if defined(_MSC_VER)
        unsigned long long xcr0 = _xgetbv(0);
#else
        unsigned lo, hi; __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
        unsigned long long xcr0 = ((unsigned long long)hi << 32) | lo;
#endif
        ymm = (xcr0 & 6) == 6;
    }
    bool avx2 = false, bmi2 = false;
    if (max_leaf >= 7) { cpuid(7, 0); avx2 = (r[1] >> 5) & 1; bmi2 = (r[1] >> 8) & 1; }
    add("avx", avx && ymm); add("avx2", avx2 && avx && ymm); add("fma", fma && avx && ymm);
    add("f16c", f16c && avx && ymm); add("bmi2", bmi2);
#elif defined(SC_CPU_ARM)
    // Numeric HWCAP bits, because not every NDK header defines the names.
#if defined(__APPLE__)
    auto has = [](const char * n) { int v = 0; size_t len = sizeof v; return sysctlbyname(n, &v, &len, nullptr, 0) == 0 && v; };
    add("dotprod", has("hw.optional.arm.FEAT_DotProd")); add("i8mm", has("hw.optional.arm.FEAT_I8MM"));
#elif defined(__linux__)
    unsigned long h = getauxval(AT_HWCAP), h2 = getauxval(AT_HWCAP2);
    add("dotprod", h & (1UL << 20)); add("i8mm", h2 & (1UL << 13)); add("sve", h & (1UL << 22));
#endif
#endif
    return out.empty() ? "none" : out;
}

static std::string sc_cpu_compiled() {
    std::string out;
    auto add = [&](const char * name, int on) { if (on) { if (!out.empty()) out += ','; out += name; } };
    add("avx", ggml_cpu_has_avx()); add("avx2", ggml_cpu_has_avx2()); add("fma", ggml_cpu_has_fma());
    add("f16c", ggml_cpu_has_f16c()); add("bmi2", ggml_cpu_has_bmi2()); add("neon", ggml_cpu_has_neon());
    add("dotprod", ggml_cpu_has_dotprod()); add("i8mm", ggml_cpu_has_matmul_int8()); add("sve", ggml_cpu_has_sve());
    return out.empty() ? "none" : out;
}

const char * sc_worker_cpu(void) {
    static const std::string report = [] {
        std::string hw = sc_cpu_hardware(), built = sc_cpu_compiled();
        return "compiled=" + built + " hardware=" + hw;
    }();
    return report.c_str();
}

int sc_worker_run(int port, uint64_t budget_bytes, int threads, const char * cache_dir) {
    if (port < 1 || port > 65535 || budget_bytes < 64ULL*1024*1024 || threads < 1 || threads > 16) return 2;
    sc_rpc_set_budget(budget_bytes);
    auto reg = ggml_backend_cpu_reg();
    if (!reg || ggml_backend_reg_dev_count(reg) != 1) return 3;
    auto dev = ggml_backend_reg_dev_get(reg, 0);
    // Lives as long as the server, which never returns while healthy.
    std::string cache = cache_dir ? cache_dir : "";
    if (!cache.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(cache, ec);
        if (ec || !std::filesystem::is_directory(cache, ec)) {
            fprintf(stderr, "ShareCompute: weight cache disabled, cannot use '%s'\n", cache.c_str());
            cache.clear();
        }
    }
    std::string endpoint = "127.0.0.1:" + std::to_string(port);
    ggml_backend_rpc_start_server(endpoint.c_str(), cache.empty() ? nullptr : cache.c_str(), threads, 1, &dev);
    return 4; // A healthy listener never returns.
}
uint64_t sc_worker_allocated(void) { return sc_rpc_allocated(); }
uint64_t sc_worker_peak(void) { return sc_rpc_peak(); }
uint64_t sc_worker_graphs(void) { return sc_rpc_graphs(); }
uint64_t sc_worker_cache_hit_bytes(void) { return sc_rpc_cache_hit_bytes(); }
uint64_t sc_worker_cache_stored_bytes(void) { return sc_rpc_cache_stored_bytes(); }
uint64_t sc_worker_cache_rejected(void) { return sc_rpc_cache_rejected(); }
const char * sc_worker_revision(void) { return SC_REVISION; }
const char * sc_worker_build(void) { return SC_BUILD_COMMIT; }
