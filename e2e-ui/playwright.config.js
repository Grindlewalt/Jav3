module.exports = {
  testDir: '.',
  timeout: 40000,
  reporter: [['list']],
  use: { headless: true, baseURL: 'http://localhost:8000', actionTimeout: 15000 },
};
