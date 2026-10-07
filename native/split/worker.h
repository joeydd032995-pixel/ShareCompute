#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// cache_dir may be NULL or empty to leave the weight cache off. It is created if missing.
int sc_worker_run(int port, uint64_t budget_bytes, int threads, const char * cache_dir);
uint64_t sc_worker_allocated(void);
uint64_t sc_worker_peak(void);
uint64_t sc_worker_graphs(void);
uint64_t sc_worker_cache_hit_bytes(void);
uint64_t sc_worker_cache_stored_bytes(void);
uint64_t sc_worker_cache_rejected(void);
const char * sc_worker_revision(void);
#ifdef __cplusplus
}
#endif
