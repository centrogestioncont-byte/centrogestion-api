// API Centro de Gestion - paso 0
// Solo responde que esta viva. Sin base de datos todavia.
// Sin dependencias: usa el modulo http que ya trae Node.
const http = require('http');
const PORT = process.env.PORT || 3000;
const ORIGENES = ['https://centrogestion.pages.dev', 'https://centrogestion-test.pages.dev'];
const server = http.createServer(function (req, res) {
var origen = req.headers.origin;
if (origen && ORIGENES.indexOf(origen) !== -1) {
res.setHeader('Access-Control-Allow-Origin', origen);
res.setHeader('Vary', 'Origin');
}
res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
if (req.method === 'OPTIONS') {
res.writeHead(204);
res.end();
return;
}
res.setHeader('Content-Type', 'application/json; charset=utf-8');
if (req.url === '/' || req.url === '/salud') {
res.writeHead(200);
res.end(JSON.stringify({ ok: true, mensaje: 'estoy viva', hora: new Date().toISOString() }));
return;
}
res.writeHead(404);
res.end(JSON.stringify({ ok: false, mensaje: 'ruta no encontrada' }));
});
server.listen(PORT, '0.0.0.0', function () {
console.log('API escuchando en el puerto ' + PORT);
});
