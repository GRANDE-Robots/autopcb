/*
 * ServerStatus.kt: the outcomes of asking a server who it is.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

/** What `/api/info` told the app about a server. */
enum class ServerStatus {
    /** No answer: the connection failed or timed out. */
    UNREACHABLE,

    /** An answer that is not AutoPCB API level 1. */
    NOT_AUTOPCB,

    /** AutoPCB, but it refused the token. */
    TOKEN_REJECTED,

    /** AutoPCB API level 1 that accepts the token. */
    READY,
}
