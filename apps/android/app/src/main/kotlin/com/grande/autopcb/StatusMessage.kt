/*
 * StatusMessage.kt: chooses the text shown for each ServerStatus.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import androidx.annotation.StringRes

/** Maps a [ServerStatus] to its message in strings.xml. */
class StatusMessage {

    /** Returns the string resource that explains [status] to the user. */
    @StringRes
    operator fun invoke(status: ServerStatus): Int = when (status) {
        ServerStatus.UNREACHABLE -> R.string.status_unreachable
        ServerStatus.NOT_AUTOPCB -> R.string.status_not_autopcb
        ServerStatus.TOKEN_REJECTED -> R.string.status_token_rejected
        ServerStatus.READY -> R.string.status_ready
    }
}
