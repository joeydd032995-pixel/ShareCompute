#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
int sc_worker_run(int port, uint64_t budget_bytes, int threads);
uint64_t sc_worker_allocated(void);
uint64_t sc_worker_peak(void);
uint64_t sc_worker_graphs(void);
const char * sc_worker_revision(void);
#ifdef __cplusplus
}
#endif
