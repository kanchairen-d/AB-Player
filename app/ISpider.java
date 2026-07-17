package com.github.tvbox.osc.spider;

import java.util.HashMap;
import java.util.List;
import android.content.Context;

public interface ISpider {
    void init(Context context, String ext) throws Exception;
    String homeContent(boolean filter) throws Exception;
    String homeVideoContent() throws Exception;
    String categoryContent(String tid, int pg, boolean filter, HashMap<String, String> extend) throws Exception;
    String detailContent(List<String> ids) throws Exception;
    String searchContent(String key, boolean quick) throws Exception;
    String playerContent(String flag, String id, HashMap<String, String> headers) throws Exception;
}