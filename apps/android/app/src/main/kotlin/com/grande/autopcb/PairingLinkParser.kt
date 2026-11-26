/*
 * PairingLinkParser.kt: turns the text of a pairing link into a PairingLink.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import java.net.URI
import java.net.URISyntaxException

/** Parses `http://HOST:PORT/#token=TOKEN` as printed by `autopcb serve`. */
class PairingLinkParser {

    /**
     * Parses one pairing link.
     *
     * Inputs: link text; surrounding whitespace is ignored. The scheme must be http,
     * the path empty or `/`, there must be no query, and the fragment must carry a
     * `token=` value. A missing port means port 80.
     * Outputs: the link, or null for anything else, including text after the link.
     */
    operator fun invoke(text: String): PairingLink? {
        val uri = try {
            URI(text.trim())
        } catch (_: URISyntaxException) {
            return null
        }
        if (uri.query != null || uri.rawPath !in setOf("", "/")) return null
        val origin = Origin.from(text.trim().substringBefore('#')) ?: return null
        val token = uri.rawFragment
            ?.split('&')
            ?.firstOrNull { it.startsWith("token=") }
            ?.removePrefix("token=")
        return if (token.isNullOrEmpty()) null else PairingLink(origin, token)
    }
}
