#include <jni.h>
#include "worker.h"
extern "C" JNIEXPORT jint JNICALL Java_com_sharecompute_worker_NativeWorker_run(JNIEnv *, jclass, jint port, jlong budget) {
    return sc_worker_run(port, static_cast<uint64_t>(budget), 2);
}
extern "C" JNIEXPORT jlongArray JNICALL Java_com_sharecompute_worker_NativeWorker_stats(JNIEnv *env, jclass) {
    jlong values[] = {static_cast<jlong>(sc_worker_allocated()), static_cast<jlong>(sc_worker_peak()), static_cast<jlong>(sc_worker_graphs())};
    auto result = env->NewLongArray(3); if (result) env->SetLongArrayRegion(result, 0, 3, values); return result;
}
extern "C" JNIEXPORT jstring JNICALL Java_com_sharecompute_worker_NativeWorker_revision(JNIEnv *env, jclass) {
    return env->NewStringUTF(sc_worker_revision());
}
