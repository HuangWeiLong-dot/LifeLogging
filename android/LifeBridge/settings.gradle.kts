// 依赖走阿里云镜像优先 —— 这台机器上 dl.google.com 不通（实测 000），
// 而 maven.aliyun.com 的 google / public 两个仓都能拿到 AGP、Compose、Kotlin。
// 官方仓留在后面做兜底：换网络环境时不用改这里。
pluginManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
    }
}

rootProject.name = "LifePassion"
include(":app")
include(":collect")
