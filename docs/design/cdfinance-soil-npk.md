# cdfinance analyzeSoilV2 NPK 集成

## 上游

- `POST https://joint-venture.cdfinance.com.cn/agric-api/agriculture/land/analyzeSoilV2`
- Body：`coords`（`lon,lat|...`）、省市区编码、`source=2`、可选 `landId`
- Headers：`Authorization: Bearer <JWT>`、`channel-net: H5`、`hr-base-id`、`x-cfpamf-app-key`

## 签名（sign）

H5/网关偶发附带 query：`timestamp`（毫秒）、`nonce`、`z_seller`（如 `knhsellerMobilejsff8pa`）、`sv=sv01`、`sign`（RSA-1024，Base64，128 字节）。

实测：**仅 Bearer + 固定 headers 即可成功**，joint-venture-front 的 axios 拦截器也不生成 sign。  
本服务支持可选 `auth_query`（调用方粘贴完整 query），不在仓库内保存私钥或 token。

若未来网关强制验签：需 knhsellerMobile 客户端签名密钥，或由前端继续粘贴预签名 query。

## 存储

表 `soil_nutrient_npk`（与 SoilGrids 的 `soil_profiles` / `soil_field_summary` 分离）。

## API

- `GET /v1/lands/{land_id}/soil/npk`
- `POST /v1/lands/{land_id}/soil/npk` — body/header 传临时 Bearer；`force=true` 刷新
