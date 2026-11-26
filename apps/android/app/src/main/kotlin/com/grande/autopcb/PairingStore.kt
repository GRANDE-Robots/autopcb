/*
 * PairingStore.kt: keeps the paired origin and token in encrypted storage.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.io.IOException
import java.security.GeneralSecurityException

/**
 * The one stored pairing, encrypted with a Keystore-backed master key.
 *
 * Inputs: any [Context]; only the application context is kept.
 */
class PairingStore(context: Context) {

    private val appContext = context.applicationContext

    private fun preferences(): SharedPreferences = EncryptedSharedPreferences.create(
        appContext,
        FILE,
        MasterKey.Builder(appContext).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    /**
     * Reads the stored pairing.
     *
     * Outputs: the pairing, or null when none is stored or the encrypted data cannot
     * be read back, in which case the app pairs again.
     */
    fun load(): PairingLink? {
        val saved = try {
            preferences().run { getString(ORIGIN, null) to getString(TOKEN, null) }
        } catch (_: GeneralSecurityException) {
            return null
        } catch (_: IOException) {
            return null
        }
        val origin = saved.first?.let { Origin.from(it) } ?: return null
        return saved.second?.let { PairingLink(origin, it) }
    }

    /** Stores [link], replacing any earlier pairing. */
    fun save(link: PairingLink) {
        preferences().edit().putString(ORIGIN, link.origin.toString()).putString(TOKEN, link.token).apply()
    }

    /** Deletes the stored pairing. */
    fun clear() {
        appContext.deleteSharedPreferences(FILE)
    }

    private companion object {
        const val FILE = "pairing"
        const val ORIGIN = "origin"
        const val TOKEN = "token"
    }
}
