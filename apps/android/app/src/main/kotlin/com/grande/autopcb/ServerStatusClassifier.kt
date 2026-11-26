/*
 * ServerStatusClassifier.kt: classifies an /api/info reply as a ServerStatus.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

package com.grande.autopcb

import org.json.JSONException
import org.json.JSONObject

/** Maps the body of a `/api/info` reply to a [ServerStatus]. */
class ServerStatusClassifier {

    /**
     * Classifies one reply.
     *
     * Inputs: the reply body, or null when the request failed with an I/O error or
     * timeout.
     * Outputs: UNREACHABLE for null; NOT_AUTOPCB for a body that is not JSON, whose
     * product is not "AutoPCB" or whose api is not 1; TOKEN_REJECTED when authorized
     * is not true; otherwise READY.
     */
    operator fun invoke(body: String?): ServerStatus {
        if (body == null) return ServerStatus.UNREACHABLE
        val info = try {
            JSONObject(body)
        } catch (_: JSONException) {
            return ServerStatus.NOT_AUTOPCB
        }
        if (info.optString("product") != "AutoPCB" || info.optInt("api") != 1) {
            return ServerStatus.NOT_AUTOPCB
        }
        return if (info.optBoolean("authorized")) ServerStatus.READY else ServerStatus.TOKEN_REJECTED
    }
}
