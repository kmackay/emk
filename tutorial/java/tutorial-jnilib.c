#include "library/library.h"

#include <jni.h>

JNIEXPORT void JNICALL Java_javatest_print_1revision_1info(JNIEnv *env, jclass clazz)
{
    print_revision_info();
}
