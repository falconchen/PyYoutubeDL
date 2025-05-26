import yt_dlp
import os



# 获取当前目录下的yt-dlp.conf路径
current_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(current_dir, 'yt-dlp.conf')
cookies_path = os.path.join(current_dir, 'firefox-cookies.txt')

ydl_opts = {
    'cookiesfrombrowser': ('chrome',),
    'cookiefile': cookies_path,  # 下载后会把cookies保存到这个文件
    'skip_download': True,  # 不下载，只提取cookie
}




def main():
    # 用一个随便的视频链接即可
    url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
        ydl.save_cookies()  # 导出cookie到文件



def filter_cookies(input_path, output_path, domains=('youtube.com', 'accounts.google.com')):
    with open(input_path, 'r', encoding='utf-8') as fin, open(output_path, 'w', encoding='utf-8') as fout:
        for line in fin:
            if line.startswith('#') or any(domain in line for domain in domains):
                fout.write(line)



if __name__ == '__main__':
    main()
    filter_cookies('firefox-cookies.txt', 'youtube-cookies.txt')

