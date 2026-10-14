#include <jni.h>
#include <stdint.h>
#include "playfair.h"

JNIEXPORT jbyteArray JNICALL
Java_com_rayren_imirrorlite_AirPlayCrypto_unwrap(JNIEnv *env, jclass cls,
                                               jbyteArray handshake, jbyteArray encrypted) {
    (void)cls;
    if ((*env)->GetArrayLength(env, handshake) != 164 ||
        (*env)->GetArrayLength(env, encrypted) != 72) {
        (*env)->ThrowNew(env, (*env)->FindClass(env, "java/lang/IllegalArgumentException"), "Invalid FairPlay key length");
        return NULL;
    }
    unsigned char message[164], input[72], output[16];
    (*env)->GetByteArrayRegion(env, handshake, 0, 164, (jbyte *)message);
    (*env)->GetByteArrayRegion(env, encrypted, 0, 72, (jbyte *)input);
    if (message[12] > 3) {
        (*env)->ThrowNew(env, (*env)->FindClass(env, "java/lang/IllegalArgumentException"), "Invalid FairPlay mode");
        return NULL;
    }
    playfair_decrypt(message, input, output);
    jbyteArray result = (*env)->NewByteArray(env, 16);
    if (result) (*env)->SetByteArrayRegion(env, result, 0, 16, (jbyte *)output);
    return result;
}
