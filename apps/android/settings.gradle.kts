/*
 * settings.gradle.kts: names the AutoPCB Android project and where Gradle finds
 * plugins and libraries.
 *
 * Copyright: GRANDE Robots and Aaron John Sabu
 */

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "autopcb-android"
include(":app")
