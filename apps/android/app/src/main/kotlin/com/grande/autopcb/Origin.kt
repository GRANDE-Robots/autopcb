/*
 * Origin.kt: the scheme, host and port a paired AutoPCB server is reached at.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import java.net.URI
import java.net.URISyntaxException

/**
 * The scheme, host and port of one server.
 *
 * Inputs: [scheme] and [host] in lower case (an IPv6 host keeps its brackets) and a
 * [port] from 1 to 65535.
 */
data class Origin(val scheme: String, val host: String, val port: Int) {

    /** Returns the origin as `scheme://host:port`. */
    override fun toString(): String = "$scheme://$host:$port"

    /**
     * Says whether [url] points at this origin.
     *
     * Inputs: any URL text. A missing port means the scheme's default port.
     * Outputs: true only when scheme, host and port all equal this origin; text that
     * is not a URL gives false.
     */
    fun contains(url: String): Boolean = from(url) == this

    companion object {
        /**
         * Reads the origin of an http URL.
         *
         * Inputs: URL text. A missing port means port 80.
         * Outputs: the origin, or null when the text is not a URL with an http scheme
         * and a host.
         */
        fun from(url: String): Origin? {
            val uri = try {
                URI(url)
            } catch (_: URISyntaxException) {
                return null
            }
            val scheme = uri.scheme?.lowercase()
            val host = uri.host?.lowercase()
            if (scheme != "http" || host == null) return null
            return Origin(scheme, host, if (uri.port == -1) 80 else uri.port)
        }
    }
}
