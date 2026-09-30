#include "worker.h"
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>

int main(int argc, char ** argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(sc_worker_revision()); return 0; }
    if (argc != 4) { fprintf(stderr, "usage: sc-rpc-worker PORT BUDGET_MIB THREADS\n"); return 2; }
    std::thread([] {
        for (;;) {
            printf("SC_STATS {\"allocated_bytes\":%llu,\"peak_bytes\":%llu,\"graph_calls\":%llu}\n",
                (unsigned long long)sc_worker_allocated(), (unsigned long long)sc_worker_peak(),
                (unsigned long long)sc_worker_graphs());
            fflush(stdout);
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
    }).detach();
    return sc_worker_run(atoi(argv[1]), strtoull(argv[2], nullptr, 10)*1024*1024, atoi(argv[3]));
}
