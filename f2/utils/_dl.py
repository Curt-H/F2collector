# path: f2/utils/_dl.py

import m3u8
import httpx
import traceback

from pathlib import Path
from typing import Union
from f2.utils.utils import ensure_path
from f2.log.logger import logger
from f2.i18n.translator import _


async def get_content_length(url: str, headers: dict = ..., proxies: dict = ...) -> int:
    """
    获取给定URL的Content-Length (Retrieve the Content-Length for a given URL)

    Args:
        url (str): 目标URL (Target URL)
        headers (dict): 请求头 (Request headers)
        proxies (dict): 代理 (Proxies)

    Returns:
        int: Content-Length的值。本地补丁(f2dl)三态语义：
            >=1 正常长度；0 服务器明确表示无内容(死链/拒绝，调用方应换链接)；
            -1 探测自身网络故障(超时/断连等瞬时错误，调用方可退避后重试)。
            (>=1 normal size; 0 server says no content (dead link);
            -1 transient probe failure, safe to retry with backoff.)
    """

    if proxies is ... or proxies is None:
        proxies = {"all://": None}

    proxy_url = (
        proxies.get("http://") or proxies.get("https://") or proxies.get("all://")
    )

    # 本地补丁(f2dl)：探测超时 10s → 30s。AsyncHTTPTransport(retries=5) 只对
    # 连接失败重试，对读取超时无效；加宽超时可显著降低 CDN 慢响应导致的误判。
    async with httpx.AsyncClient(
        timeout=30.0,
        transport=httpx.AsyncHTTPTransport(retries=5, proxy=proxy_url),
        verify=False,
    ) as aclient:
        try:
            response = await aclient.head(url, headers=headers, follow_redirects=True)
            # 当head请求被禁止时，释放status异常被捕获 (When head requests are forbidden, release status exceptions are caught)
            response.raise_for_status()

            if (
                response.headers.get("Content-Length") != None
                and int(response.headers.get("Content-Length")) == 0
            ):
                # 如果head请求无法获取Content-Length, 则使用GET请求再次尝试获取
                response = await aclient.get(
                    url, headers=headers, follow_redirects=True
                )
                response.raise_for_status()

        except httpx.TransportError:
            # 本地补丁(f2dl)：探测自身网络故障(连接/读取超时、断连等瞬时错误)
            # 返回 -1，交由调用方退避后就地重试，不再误判为死链。
            # (Transient probe network failure: return -1 so the caller can
            # retry with backoff instead of treating the link as dead.)
            logger.error(traceback.format_exc())
            logger.error(_("探测文件大小时网络错误: {0}".format(url)))
            logger.debug("===================================")
            logger.debug(f"headers:{headers}, proxies:{proxies}")
            logger.debug("===================================")
            return -1
        # 对HTTP状态错误进行处理 (Handling HTTP status errors)
        except httpx.HTTPStatusError as exc:
            # HEAD或请求不被允许 (HEAD or request not allowed)
            if exc.response.status_code in [405, 403, 401, 302]:
                try:
                    # 使用GET请求尝试再次获取Content-Length
                    # (Trying to retrieve Content-Length using GET request)
                    request = aclient.build_request("GET", url, headers=headers)
                    # 使用stream=True来避免下载整个内容
                    # (Using stream=True to avoid downloading the entire content)
                    response = await aclient.send(
                        request, stream=True, follow_redirects=True
                    )
                    response.raise_for_status()
                # 本地补丁(f2dl)：GET 回退遇到瞬时网络错误 → -1 可重试
                except httpx.TransportError as e:
                    logger.error(traceback.format_exc())
                    logger.error(
                        _(
                            "HTTP状态错误, 尝试GET请求失败: {0}, 错误详情: {1}".format(
                                url, e
                            )
                        )
                    )
                    return -1
                except Exception as e:
                    logger.error(traceback.format_exc())
                    logger.error(
                        _(
                            "HTTP状态错误, 尝试GET请求失败: {0}, 错误详情: {1}".format(
                                url, e
                            )
                        )
                    )
                    return 0
            else:
                logger.error(
                    _(
                        "HTTP状态错误: {0}, 状态码: {1}".format(
                            url, exc.response.status_code
                        )
                    )
                )
                return 0
        except httpx.RequestError as e:
            # 本地补丁(f2dl)：请求层错误(TransportError 之外的 TooManyRedirects 等)
            # 同样视为瞬时故障 → -1 可重试
            logger.error(traceback.format_exc())
            logger.error(_("httpx 请求错误：{0}，错误详情：{1}".format(url, e)))
            return -1
        except Exception as e:
            # 处理未知错误 (Handling unknown errors)
            # 本地补丁(f2dl)：未知错误就地重试成本低，同样返回 -1 交由调用方重试
            logger.error(traceback.format_exc())
            logger.error(
                _(
                    "f2 请求 Content-Length 时发生未知错误: {0}, 错误详情: {1}".format(
                        url, e
                    )
                )
            )
            return -1

        # 返回Content-Length值 (Returning the Content-Length value)
        return int(response.headers.get("Content-Length", 0))

        # raise ValueError("响应中没有找到Content-Length") # Content-Length header not found in the response


def trim_filename(filename: Union[str, Path], max_length: int = 50) -> str:
    """
    裁剪文件名以适应控制台显示 (Trim the filename to fit console display)

    Args:
        filename (str or Path): 完整的文件名 (Full filename)
        max_length (int): 显示的最大字符数 (Maximum number of characters to display)

    Returns:
        裁剪后的文件名 (trimmed filename)
    """

    filename = str(ensure_path(filename))

    prefix_suffix_len = max_length // 2 - 2

    # 如果文件名长度超过最大长度，则进行裁剪
    return (
        f"{filename[:prefix_suffix_len]}...{filename[-prefix_suffix_len:]}"
        if len(str(filename)) > max_length
        else filename
    )


def get_chunk_size(file_size: int) -> int:
    """
    根据文件大小确定合适的下载块大小 (Determine appropriate download chunk size based on file size)

    Args:
        file_size (int): 文件大小，单位为字节 (File size in bytes)

    Returns:
        int: 下载块的大小 (Size of the download chunk)
    """

    # 文件大小单位为字节 (File size is in bytes)
    if file_size < 10 * 1024:  # 小于10KB (Less than 10KB)
        return file_size  # 一次性下载整个文件 (Download the entire file at once)
    elif file_size < 1 * 1024 * 1024:  # 小于1MB (Less than 1MB)
        return file_size // 10
    elif file_size < 10 * 1024 * 1024:  # 小于10MB (Less than 10MB)
        return file_size // 20
    elif file_size < 100 * 1024 * 1024:  # 小于100MB (Less than 100MB)
        return file_size // 50
    else:  # 文件大小大于100MB (File size greater than 100MB)
        return 1 * 1024 * 1024  # 使用1MB的块大小 (Use a chunk size of 1MB)


async def get_segments_from_m3u8(url: str) -> Union[list, str, None]:
    """
    从给定的m3u8文件中获取segments

    Args:
        url (str): m3u8文件的URL

    Returns:
        m3u8文件中的segments列表
    """
    m3u8_obj = m3u8.load(url)
    segments = m3u8_obj.segments

    # 如果没有segments说明m3u8可能存在嵌套, 需要尝试获取嵌套的m3u8文件
    # (If there are no segments, the m3u8 may be nested and
    # you need to try to get the nested m3u8 file)
    if not segments:
        logger.debug(_("未找到m3u8文件的segments, 尝试获取嵌套的m3u8文件"))
        # 尝试获取嵌套的m3u8文件 (Try to get the nested m3u8 file)
        nested_m3u8_url = m3u8_obj.playlists[0].absolute_uri
        segments = await get_segments_from_m3u8(nested_m3u8_url)
        # 再次检查segments是否存在 (Check again if segments exist)
        if not segments:
            logger.error(
                _(
                    "未找到嵌套m3u8文件的segments, 可能直播结束或该主播无法使用m3u8方法解析"
                )
            )
    return segments


async def get_segments_duration(url: str) -> Union[list, int, float, None]:
    """
    从给定的m3u8文件中获取segments的duration

    Args:
        url (str): m3u8文件的URL

    Returns:
        segments的duration列表
    """
    segments = await get_segments_from_m3u8(url)
    return [segment.duration for segment in segments]
