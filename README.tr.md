# VR Monitor

VR gözlüğe takılan telefonu Windows bilgisayarın sanal monitörlerine çevirir. PC ekranlarını (gerçek ve sanal)
USB (adb reverse) veya Wi-Fi ile yayınlar; Android uygulaması bunları baş takipli, stereo paneller olarak gösterir.
Web kamerası ile el takibi ve DualShock desteği vardır.

## Kurulum
Son sürümden `VR.Monitor.Setup.<sürüm>.exe` ve `VRMonitor.apk` dosyalarını indirin (veya Sentinel Hub ile kurun).
APK'yı telefona kurun, USB hata ayıklama açık şekilde bağlayın ve PC'de VR Monitor'ü başlatın.

## Sürümleme
`VERSION` dosyası tek kaynaktır; etiket `v<VERSION>`. Sentinel Hub güncellemeleri sürüm etiketlerini karşılaştırarak bulur.
`scripts/release.ps1 -Bump patch|minor|major` ile yayınlanır.

Lisans: MIT
