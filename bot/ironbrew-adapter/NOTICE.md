# Ironbrew adapter notice

`Program.cs` is a small project-owned command-line adapter. At build time, Docker fetches the unmodified upstream project [Lethal-Luka/IronbrewDeobfuscator](https://github.com/Lethal-Luka/IronbrewDeobfuscator) at commit `43dfbd8fcff52c1f0b9d1ad32ae45142c03683d4` and references its public `Deobfuscation` API.

The upstream project is distributed under GNU GPL version 3. This linked adapter is also provided under GPL-3.0; its full license is in `LICENSE-GPL-3.0.txt` and is copied into `/app/licenses/IRONBREW-GPL-3.0.txt` in the runtime image. The upstream source is available at the pinned public commit. This adapter invokes the library API and does not include or modify upstream source files.
