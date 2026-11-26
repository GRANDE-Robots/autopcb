/*
 * PairingLink.kt: a paired server's origin and access token.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

/**
 * What pairing yields: where the server is and the token that logs in.
 *
 * Inputs: the server [origin] and its non-empty [token].
 */
data class PairingLink(val origin: Origin, val token: String) {

    /** The page address that logs the web page in: `origin/#token=token`. */
    val loginUrl: String get() = "$origin/#token=$token"

    /** Hides the token so it never reaches a log. */
    override fun toString(): String = "PairingLink($origin)"
}
