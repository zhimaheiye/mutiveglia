#!/bin/bash
# Veglia · Android build. Copyright (c) 2026 Evelyn & River — CC BY-NC-SA 4.0.
#
# Builds an UNSIGNED apk, then signs it with a DEBUG keystore it generates on
# the fly. The debug keystore/password below are throwaway demo values — fine
# for installing on your own phone, NOT for distribution. Generate your own key
# for anything you share:  keytool -genkeypair -keystore my.jks -alias veglia ...
set -e

# --- adjust these to your machine -------------------------------------------
export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}
export ANDROID_HOME=${ANDROID_HOME:-$HOME/android-sdk}
PLATFORM=$ANDROID_HOME/platforms/android-34/android.jar
BUILD_TOOLS=$ANDROID_HOME/build-tools/34.0.0
# ----------------------------------------------------------------------------

PROJECT="$(cd "$(dirname "$0")" && pwd)"
SRC=$PROJECT/app/src/main
OUT=$PROJECT/build
PKG_PATH=dev/veglia/companion

rm -rf $OUT
mkdir -p $OUT/gen $OUT/classes $OUT/apk $OUT/compiled_res

echo "=== Compiling resources ==="
$BUILD_TOOLS/aapt2 compile --dir $SRC/res -o $OUT/compiled_res/

echo "=== Linking resources ==="
$BUILD_TOOLS/aapt2 link \
    -o $OUT/apk/app.unsigned.apk \
    -I $PLATFORM \
    --manifest $SRC/AndroidManifest.xml \
    --java $OUT/gen \
    --auto-add-overlay \
    -R $OUT/compiled_res/*.flat

echo "=== Compiling Java ==="

# Windows Git Bash compatibility:
# javac.exe cannot correctly consume MSYS paths such as /d/veglia/...
# Convert every Java source and javac path to native Windows paths.

find "$SRC/java" -name "*.java" | while IFS= read -r file; do
    cygpath -w "$file"
done > "$OUT/sources.txt"

cygpath -w "$OUT/gen/$PKG_PATH/R.java" >> "$OUT/sources.txt"

PLATFORM_WIN="$(cygpath -w "$PLATFORM")"
CLASSES_WIN="$(cygpath -w "$OUT/classes")"
SOURCES_WIN="$(cygpath -w "$OUT/sources.txt")"

javac \
    -encoding UTF-8 \
    -source 11 -target 11 \
    -classpath "$PLATFORM_WIN" \
    -d "$CLASSES_WIN" \
    @"$SOURCES_WIN"

echo "=== Creating DEX ==="

# Windows Git Bash compatibility:
# Android SDK on Windows provides d8.bat / apksigner.bat.
# Convert paths passed into Windows-native tools.

APK_OUT_WIN="$(cygpath -w "$OUT/apk")"
PLATFORM_WIN="$(cygpath -w "$PLATFORM")"

CLASS_FILES_WIN=()
while IFS= read -r file; do
    CLASS_FILES_WIN+=("$(cygpath -w "$file")")
done < <(find "$OUT/classes" -name "*.class")

"$BUILD_TOOLS/d8.bat" \
    --output "$APK_OUT_WIN" \
    --lib "$PLATFORM_WIN" \
    "${CLASS_FILES_WIN[@]}"

echo "=== Building APK ==="
cd "$OUT/apk"

cp app.unsigned.apk app.tmp.apk

JAR_TOOL="$JAVA_HOME/bin/jar.exe"

if [ ! -f "$JAR_TOOL" ]; then
    echo "ERROR: jar.exe not found: $JAR_TOOL"
    exit 1
fi

# 将 D8 生成的 classes.dex 加入/替换到 APK 根目录
"$JAR_TOOL" uf app.tmp.apk classes.dex

mv app.tmp.apk app.unsigned.apk

echo "=== Generating DEBUG signing key (throwaway; replace for release) ==="

DEBUG_KS="$PROJECT/debug.jks"
DEBUG_KS_WIN="$(cygpath -w "$DEBUG_KS")"

if [ ! -f "$DEBUG_KS" ]; then
    keytool -genkeypair -v \
        -keystore "$DEBUG_KS_WIN" \
        -keyalg RSA -keysize 2048 \
        -validity 10000 \
        -alias veglia \
        -storepass veglia-debug \
        -keypass veglia-debug \
        -dname "CN=Veglia Debug"
fi

echo "=== Aligning ==="

"$BUILD_TOOLS/zipalign.exe" \
    -f 4 \
    app.unsigned.apk \
    app.aligned.apk

echo "=== Signing (debug) ==="

APK_FINAL_WIN="$(cygpath -w "$PROJECT/Veglia.apk")"
APK_ALIGNED_WIN="$(cygpath -w "$OUT/apk/app.aligned.apk")"

"$BUILD_TOOLS/apksigner.bat" sign \
    --ks "$DEBUG_KS_WIN" \
    --ks-pass pass:veglia-debug \
    --key-pass pass:veglia-debug \
    --ks-key-alias veglia \
    --out "$APK_FINAL_WIN" \
    "$APK_ALIGNED_WIN"

echo ""
echo "=== Done! ==="
echo "APK: $PROJECT/Veglia.apk"
ls -lh "$PROJECT/Veglia.apk"
