#include <jni.h>
#include <string>
#include "worker.h"
extern "C" JNIEXPORT jint JNICALL Java_com_sharecompute_worker_NativeWorker_run(JNIEnv * env, jclass, jint port, jlong budget, jstring cacheDir) {
    std::string cache;
    if (cacheDir) {
        const char * chars = env->GetStringUTFChars(cacheDir, nullptr);
        if (chars) { cache = chars; env->ReleaseStringUTFChars(cacheDir, chars); }
    }
    // sc_worker_run never returns while healthy, so `cache` outlives the server that uses it.
    return sc_worker_run(port, static_cast<uint64_t>(budget), 2, cache.c_str());
}
extern "C" JNIEXPORT jlongArray JNICALL Java_com_sharecompute_worker_NativeWorker_stats(JNIEnv *env, jclass) {
    jlong values[] = {static_cast<jlong>(sc_worker_allocated()), static_cast<jlong>(sc_worker_peak()), static_cast<jlong>(sc_worker_graphs()),
                      static_cast<jlong>(sc_worker_cache_hit_bytes()), static_cast<jlong>(sc_worker_cache_stored_bytes()),
                      static_cast<jlong>(sc_worker_cache_rejected())};
    auto result = env->NewLongArray(6); if (result) env->SetLongArrayRegion(result, 0, 6, values); return result;
}
extern "C" JNIEXPORT jstring JNICALL Java_com_sharecompute_worker_NativeWorker_revision(JNIEnv *env, jclass) {
    return env->NewStringUTF(sc_worker_revision());
}
extern "C" JNIEXPORT jstring JNICALL Java_com_sharecompute_worker_NativeWorker_buildCommit(JNIEnv *env, jclass) {
    const char * build = sc_worker_build();
    return env->NewStringUTF(build && *build ? build : "unknown");
}
