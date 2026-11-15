import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// 沿用原来的签名与版本号来源：新版是旧 LifeBridge 的**扩展**，
// 不是另一个 App。包名相同 + 签名相同 → `adb install -r` 直接覆盖安装，
// 通知监听授权和已有设置都不用重来。
val keystoreProps = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

val versionProps = Properties().apply {
    val f = rootProject.file("version.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    // 命名空间只影响 R 类与生成的 BuildConfig，与安装身份无关
    namespace = "com.lifelogging.lifepassion"
    compileSdk = 36

    defaultConfig {
        // ⚠️ 必须和旧的 LifeBridge 一致，否则无法覆盖安装
        applicationId = "com.lifelogging.bridge"
        minSdk = 26
        targetSdk = 36
        versionCode = (versionProps.getProperty("versionCode") ?: "12").toInt() + 1
        versionName = "2.0"
    }

    signingConfigs {
        create("release") {
            val path = keystoreProps.getProperty("storeFile")
            if (path != null) {
                storeFile = rootProject.file(path)
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        // ⚠️ debug 也用同一把正式签名。否则 debug 包和已经装在手机上的
        //    release 包签名不同，`installDebug` 会报 UPDATE_INCOMPATIBLE，
        //    每次改一点就要卸载重装 —— 这是「只能用微信传 APK」的另一半原因。
        debug {
            signingConfig = signingConfigs.getByName("release")
        }
        release {
            // 真机流畅度的最大杠杆：不做 R8 的 Compose 应用在低端机上
            // 明显掉帧。这里同时开混淆和资源压缩。
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            signingConfig = signingConfigs.getByName("release")
        }
    }

    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation(project(":collect"))
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
}
