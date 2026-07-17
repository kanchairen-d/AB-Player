package com.github.tvbox.osc.spider;

import android.content.Context;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.HashMap;
import java.util.List;

public class ABPlayerProxy implements ISpider {

    private String apiUrl = "http://192.168.1.11:5081/api?";
    private String userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";

    @Override
    public void init(Context context, String ext) throws Exception {
        if (ext != null && !ext.isEmpty()) {
            apiUrl = ext;
            if (!apiUrl.endsWith("?") && !apiUrl.endsWith("&")) {
                apiUrl += apiUrl.contains("?") ? "&" : "?";
            }
        }
    }

    private String httpGet(String urlStr) throws Exception {
        URL url = new URL(urlStr);
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod("GET");
        conn.setRequestProperty("User-Agent", userAgent);
        conn.setConnectTimeout(15000);
        conn.setReadTimeout(15000);
        BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"));
        StringBuilder sb = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            sb.append(line);
        }
        reader.close();
        return sb.toString();
    }

    @Override
    public String homeContent(boolean filter) throws Exception {
        String json = httpGet(apiUrl + "ac=list");
        return json;
    }

    @Override
    public String homeVideoContent() throws Exception {
        return "{\"list\":[]}";
    }

    @Override
    public String categoryContent(String tid, int pg, boolean filter, HashMap<String, String> extend) throws Exception {
        String url = apiUrl + "ac=videolist&t=" + URLEncoder.encode(tid, "UTF-8") + "&pg=" + pg;
        return httpGet(url);
    }

    @Override
    public String detailContent(List<String> ids) throws Exception {
        if (ids == null || ids.isEmpty()) return "{\"list\":[]}";
        String id = ids.get(0);
        String url = apiUrl + "ac=detail&ids=" + URLEncoder.encode(id, "UTF-8");
        return httpGet(url);
    }

    @Override
    public String searchContent(String key, boolean quick) throws Exception {
        if (key == null || key.isEmpty()) return "{\"list\":[]}";
        String url = apiUrl + "ac=search&wd=" + URLEncoder.encode(key, "UTF-8");
        return httpGet(url);
    }

    @Override
    public String playerContent(String flag, String id, HashMap<String, String> headers) throws Exception {
        StringBuilder json = new StringBuilder();
        json.append("{\"urls\":[{\"url\":\"").append(escapeJson(id));
        json.append("\"}],\"flag\":\"").append(escapeJson(flag));
        json.append("\",\"header\":{},\"parse\":0}");
        return json.toString();
    }

    private String escapeJson(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");
    }
}