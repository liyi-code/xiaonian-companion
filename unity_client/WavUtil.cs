// WavUtil.cs
// 把小念大脑推来的 wav 字节（16bit PCM）解码成 Unity AudioClip（无需外部库）。
// Python 端 tts 产出的是标准 wav；这里只支持常见 16bit/单声道或立体声。
using System;
using UnityEngine;

public static class WavUtil
{
    public static AudioClip ToAudioClip(byte[] wav)
    {
        // RIFF 头校验
        if (wav == null || wav.Length < 44 || wav[0] != 'R' || wav[1] != 'I' || wav[2] != 'F' || wav[3] != 'F')
            throw new ArgumentException("不是合法 wav");

        int channels = BitConverter.ToInt16(wav, 22);
        int sampleRate = BitConverter.ToInt32(wav, 24);
        int bitsPerSample = BitConverter.ToInt16(wav, 34);

        // 定位 data 块
        int pos = 12;
        int dataPos = -1, dataLen = 0;
        while (pos + 8 <= wav.Length)
        {
            string tag = System.Text.Encoding.ASCII.GetString(wav, pos, 4);
            int size = BitConverter.ToInt32(wav, pos + 4);
            if (tag == "data") { dataPos = pos + 8; dataLen = size; break; }
            pos += 8 + size + (size % 2);   // 块对齐
        }
        if (dataPos < 0) throw new ArgumentException("wav 缺少 data 块");

        int samples = dataLen / (bitsPerSample / 8) / channels;
        float[] data = new float[samples * channels];

        if (bitsPerSample == 16)
        {
            for (int i = 0; i < samples * channels; i++)
            {
                short s = BitConverter.ToInt16(wav, dataPos + i * 2);
                data[i] = s / 32768f;
            }
        }
        else if (bitsPerSample == 8)
        {
            for (int i = 0; i < samples * channels; i++)
                data[i] = (wav[dataPos + i] - 128) / 128f;
        }
        else
            throw new ArgumentException("仅支持 8/16 bit wav");

        var clip = AudioClip.Create("npc_wav", samples, channels, sampleRate, false);
        clip.SetData(data, 0);
        return clip;
    }
}
