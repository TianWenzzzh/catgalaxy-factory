# 更新日志

## v1.1.0（2026-09-18）

模板基线升级 **v2.9 全特性 + 懒加载首屏优化**（T6 收敛 F2–F5）：

- **默认引擎切 v29**：新学校在线生成即享开放模板 v28 全特性（极光开场/影廊灯箱/
  本命猫/喵星护照/分享卡/季节特效/全链路署名）+ 照片懒加载（首屏仅底图关键片，
  ≤0.6MB）。旧 717 行模板降为 `--engine v1`（deprecated，保留一个版本周期）。
- **共享渲染层** `app/starmap_render.py`：与 meow-starmap `tools/build.py` 同构，
  对 `schools/nuc` 数据包的输出与 16 仓托管产物**逐字节一致**
  （证据：`docs/验收证据-F5-数据包字节对齐.md`）。
- **双形态懒加载**：inline 走 00 底图关键片 + 01..N 按需取片；relative 走相对
  路径直引（零分片，file:// 双击可用）。
- **引擎选择三入口**：`GenerateRequest.engine` / `STARMAP_ENGINE` 环境变量 /
  `acceptance --engine`；`photo_loading` 参数支持 lazy|eager|relative。
- **F11 主题/校徽**：空值与旧产物字节一致；主题只接受服务端规范化 CSS，
  校徽保持 `logo_tag()` 白名单整标签。
- **质量证据**：测试 730 → 747 不降级；full76 满血验收（v29 引擎）通过；
  v29 对 v2.7 基线逐字段比对 + 数据包字节对齐；CI 新增 ubuntu 浏览器冒烟 job
  （`scripts/smoke_v29.py`，http + file:// 双协议）。
- 在线侧安全：自由文本按落地上下文转义（HTML/JS 双规则，合法取值恒等），
  恶意校名/校徽注入被就地拆解。

## v1.0.0（2026-09-15）

首个公开版本：FastAPI 在线生成器，717 行紧凑模板（v2.6 血统），730 条测试，
CI 双系统绿。
