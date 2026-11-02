# Maintainer: Obscuratea
pkgname=solocups
pkgver=2.0.0
pkgrel=1
pkgdesc='Simple, zero-headache printer manager for CUPS'
arch=('any')
url='https://github.com/Obscuratea/SoloCUPS'
license=('MIT')
depends=('python' 'python-pyqt6' 'cups' 'polkit')
optdepends=('avahi: find Wi-Fi printers'
            'nss-mdns: find Wi-Fi printers'
            'ipp-usb: driverless USB printers')
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
  cd "SoloCUPS-$pkgver"
  install -Dm755 solocups.py "$pkgdir/usr/bin/solocups"
  install -Dm644 packaging/solocups.desktop "$pkgdir/usr/share/applications/solocups.desktop"
  install -Dm644 icons/solocups.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/solocups.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
